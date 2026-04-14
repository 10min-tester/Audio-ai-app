from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import shutil
import os
import uuid
import json
import re
from datetime import datetime, timezone
from urllib import request as urllib_request
from urllib import error as urllib_error
from processing import process_audio, analyze_audio, build_processing_plan
from fastapi import BackgroundTasks

tasks_status = {}

app = FastAPI(title="Audio Restoration API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Analysis-LUFS", "X-Analysis-Crest", "X-Analysis-Phase"],  # [수정] CORS에서 헤더 노출 명시
)

TEMP_DIR = "temp_audio"
os.makedirs(TEMP_DIR, exist_ok=True)
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
PROCESS_LOG_PATH = os.path.join(LOG_DIR, "processing_history.jsonl")


def _append_process_log(entry: dict):
    try:
        with open(PROCESS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        # 로깅 실패가 메인 처리 흐름을 막지 않게 함
        pass


def _read_recent_logs(limit: int = 200) -> list[dict]:
    if not os.path.exists(PROCESS_LOG_PATH):
        return []
    try:
        with open(PROCESS_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []

    rows = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _summarize_quality(rows: list[dict]) -> dict:
    total = len(rows)
    if total == 0:
        return {
            "total_jobs": 0,
            "done_jobs": 0,
            "error_jobs": 0,
            "fallback_jobs": 0,
            "fallback_ratio": 0.0,
            "plan_source_counts": {},
            "preset_quality": [],
            "recommendation": None
        }

    done_rows = [r for r in rows if r.get("status") == "done"]
    error_rows = [r for r in rows if r.get("status") == "error"]
    fallback_rows = [r for r in rows if str(r.get("plan_source", "")).startswith("rule_based")]

    source_counts = {}
    for r in rows:
        s = str(r.get("plan_source", "unknown"))
        source_counts[s] = source_counts.get(s, 0) + 1

    # preset별 품질 스코어 집계 (사용자 피드백 반영)
    grouped = {}
    for r in done_rows:
        preset = str(r.get("preset", "unknown"))
        metrics = r.get("metrics") or {}
        lufs = float(metrics.get("LUFS", 0.0))
        crest = float(metrics.get("Crest_dB", 0.0))
        phase = float(metrics.get("Phase_Corr", 0.0))
        true_peak = float(metrics.get("TruePeak_dBTP", 0.0))
        feedback = str(r.get("user_feedback", ""))
        # 낮을수록 좋은 페널티 기반 점수
        score = abs(lufs + 14.0)
        if crest < 8.0:
            score += (8.0 - crest) * 0.8
        if phase < 0.2:
            score += (0.2 - phase) * 6.0
        if true_peak > -1.0:
            score += (true_peak + 1.0) * 2.0
        if feedback == "good":
            score -= 0.8
        elif feedback == "bad":
            score += 1.2

        acc = grouped.setdefault(preset, {"count": 0, "score_sum": 0.0, "lufs_sum": 0.0, "crest_sum": 0.0, "phase_sum": 0.0, "feedback_good": 0, "feedback_bad": 0})
        acc["count"] += 1
        acc["score_sum"] += score
        acc["lufs_sum"] += lufs
        acc["crest_sum"] += crest
        acc["phase_sum"] += phase
        if feedback == "good":
            acc["feedback_good"] += 1
        elif feedback == "bad":
            acc["feedback_bad"] += 1

    preset_quality = []
    for preset, acc in grouped.items():
        c = max(acc["count"], 1)
        preset_quality.append({
            "preset": preset,
            "jobs": acc["count"],
            "avg_quality_score": round(acc["score_sum"] / c, 3),
            "avg_lufs": round(acc["lufs_sum"] / c, 2),
            "avg_crest_db": round(acc["crest_sum"] / c, 2),
            "avg_phase_corr": round(acc["phase_sum"] / c, 2),
            "feedback_good": acc["feedback_good"],
            "feedback_bad": acc["feedback_bad"],
        })

    preset_quality.sort(key=lambda x: (x["avg_quality_score"], -x["jobs"]))
    recommendation = preset_quality[0]["preset"] if preset_quality else None

    return {
        "total_jobs": total,
        "done_jobs": len(done_rows),
        "error_jobs": len(error_rows),
        "fallback_jobs": len(fallback_rows),
        "fallback_ratio": round(len(fallback_rows) / total, 3),
        "plan_source_counts": source_counts,
        "preset_quality": preset_quality,
        "recommendation": recommendation
    }


def _sanitize_processing_plan(plan: dict | None) -> dict | None:
    if not isinstance(plan, dict):
        return None
    rules = {
        "super_res_mix_mul": (0.5, 1.6),
        "noise_reduction_mul": (0.6, 1.5),
        "brilliance_mul": (0.6, 1.5),
        "stereo_mul": (0.5, 1.3),
        "limiter_mul": (0.5, 1.3),
        "target_lufs_offset": (-2.0, 2.0),
        "deesser_mul": (0.7, 1.5),
        "highpass_offset_hz": (-30.0, 40.0),
    }
    sanitized = {}
    for key, (lo, hi) in rules.items():
        if key in plan:
            try:
                val = float(plan[key])
                if val < lo:
                    val = lo
                if val > hi:
                    val = hi
                sanitized[key] = val
            except (TypeError, ValueError):
                continue
    return sanitized


def _extract_plan_from_gemini_response(parsed: dict) -> dict | None:
    candidates = parsed.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    content = candidates[0].get("content", {})
    parts = content.get("parts", []) if isinstance(content, dict) else []
    texts = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])
    if not texts:
        return None
    raw_text = "".join(texts).strip()
    candidates_to_parse = [raw_text]

    # ```json ... ``` 코드블록 응답 대응
    fenced_matches = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", raw_text, flags=re.IGNORECASE)
    candidates_to_parse.extend(fenced_matches)

    # 문자열 내 첫 JSON object 블록 추출 시도
    first_obj_match = re.search(r"\{[\s\S]*\}", raw_text)
    if first_obj_match:
        candidates_to_parse.append(first_obj_match.group(0))

    for candidate in candidates_to_parse:
        try:
            parsed_text = json.loads(candidate)
            if isinstance(parsed_text, dict):
                if isinstance(parsed_text.get("processing_plan"), dict):
                    return _sanitize_processing_plan(parsed_text["processing_plan"])
                # 모델이 processing_plan 래퍼 없이 바로 플랜만 반환한 경우
                expected_keys = {
                    "super_res_mix_mul",
                    "noise_reduction_mul",
                    "brilliance_mul",
                    "stereo_mul",
                    "limiter_mul",
                    "target_lufs_offset"
                }
                if any(k in parsed_text for k in expected_keys):
                    return _sanitize_processing_plan(parsed_text)
        except json.JSONDecodeError:
            continue
    return None


def _call_google_plan_api(analysis: dict, preferred_preset: str) -> tuple[dict | None, str | None]:
    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return None, "missing_google_api_key"

    primary_model = os.getenv("GOOGLE_MODEL", "gemini-1.5-flash").strip()
    fallback_models_env = os.getenv("GOOGLE_MODEL_FALLBACKS", "gemini-2.0-flash,gemini-1.5-flash")
    model_candidates = [primary_model] + [m.strip() for m in fallback_models_env.split(",") if m.strip()]
    # dedupe while keeping order
    model_candidates = list(dict.fromkeys(model_candidates))

    endpoint_base = os.getenv("GOOGLE_PLAN_ENDPOINT", "https://generativelanguage.googleapis.com/v1beta/models").strip()
    prompt_payload = {
        "analysis": analysis,
        "preferred_preset": preferred_preset
    }

    request_body = {
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "You are an audio mastering planner. "
                            "Return ONLY JSON object: {\"processing_plan\": {...}} "
                            "with keys: super_res_mix_mul, noise_reduction_mul, brilliance_mul, "
                            "stereo_mul, limiter_mul, target_lufs_offset. "
                            f"Input JSON: {json.dumps(prompt_payload, ensure_ascii=True)}"
                        )
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    last_error = None
    for model in model_candidates:
        endpoint = f"{endpoint_base}/{model}:generateContent?key={api_key}"
        req = urllib_request.Request(
            endpoint,
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Content-Type": "application/json"
            },
            method="POST"
        )
        try:
            with urllib_request.urlopen(req, timeout=12) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            extracted = _extract_plan_from_gemini_response(parsed)
            if extracted is None:
                last_error = f"google_parse_failed_{model}"
                continue
            return extracted, None
        except urllib_error.HTTPError as e:
            code = getattr(e, "code", None)
            if code == 401 or code == 403:
                return None, "google_unauthorized"
            if code == 429:
                last_error = f"google_quota_exceeded_{model}"
                continue
            if code == 404:
                last_error = f"google_model_not_found_{model}"
                continue
            if code == 400:
                return None, "google_bad_request"
            last_error = f"google_http_{code or 'error'}_{model}"
            continue
        except urllib_error.URLError:
            return None, "google_network_error"
        except TimeoutError:
            return None, "google_timeout"
        except json.JSONDecodeError:
            last_error = f"google_invalid_json_{model}"
            continue
    return None, (last_error or "google_all_models_failed")


def build_processing_plan_with_fallback(analysis: dict, preferred_preset: str) -> tuple[dict, str, str | None]:
    # 1) default: local rule-based planner
    fallback_plan = build_processing_plan(analysis=analysis, preferred_preset=preferred_preset)
    last_error = None

    # 2) optional: Google Gemini planner
    google_plan, google_error = _call_google_plan_api(analysis, preferred_preset)
    if isinstance(google_plan, dict):
        return google_plan, "external_ai_google", None
    if google_error:
        last_error = google_error

    # Google API 키가 설정되어 있으면 custom planner는 타지 않음
    # (custom_invalid_json이 원인을 가리는 상황 방지)
    if os.getenv("GOOGLE_API_KEY", "").strip():
        return fallback_plan, "rule_based_fallback", (last_error or "google_fallback")

    # 3) optional: custom external AI planner
    plan_url = os.getenv("AUDIO_AI_PLAN_URL", "").strip()
    if not plan_url:
        if last_error:
            return fallback_plan, "rule_based_fallback", last_error
        return fallback_plan, "rule_based", None

    payload = json.dumps({
        "analysis": analysis,
        "preferred_preset": preferred_preset
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("AUDIO_AI_PLAN_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = urllib_request.Request(plan_url, data=payload, headers=headers, method="POST")
        with urllib_request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8")
        parsed = json.loads(body)
        ai_plan = parsed.get("processing_plan")
        ai_plan = _sanitize_processing_plan(ai_plan)
        if isinstance(ai_plan, dict) and ai_plan:
            return ai_plan, "external_ai", None
        return fallback_plan, "rule_based_fallback", "custom_invalid_plan"
    except urllib_error.HTTPError as e:
        return fallback_plan, "rule_based_fallback", f"custom_http_{getattr(e, 'code', 'error')}"
    except urllib_error.URLError:
        return fallback_plan, "rule_based_fallback", "custom_network_error"
    except TimeoutError:
        return fallback_plan, "rule_based_fallback", "custom_timeout"
    except json.JSONDecodeError:
        return fallback_plan, "rule_based_fallback", "custom_invalid_json"

    return fallback_plan, "rule_based_fallback", (last_error or "unknown_fallback")

@app.get("/api/status/{task_id}")
def get_status(task_id: str):
    return tasks_status.get(task_id, {"status": "not_found"})

@app.get("/api/download/{task_id}")
def download(task_id: str):
    task = tasks_status.get(task_id)

    if not task or task["status"] != "done":
        return {"error": "Not ready"}

    return FileResponse(task["output"], filename="restored.wav")

@app.get("/api/health")
async def health_check():
    return {"message": "Audio Restoration Backend Running"}


@app.get("/api/insights")
async def get_quality_insights(limit: int = 200):
    safe_limit = int(max(20, min(limit, 2000)))
    rows = _read_recent_logs(limit=safe_limit)
    summary = _summarize_quality(rows)
    return {"summary": summary}


@app.post("/api/feedback")
async def submit_feedback(
    task_id: str = Form(...),
    feedback: str = Form(...)
):
    normalized = feedback.strip().lower()
    if normalized not in ("good", "neutral", "bad"):
        raise HTTPException(status_code=400, detail="feedback must be one of: good, neutral, bad")

    if not os.path.exists(PROCESS_LOG_PATH):
        raise HTTPException(status_code=404, detail="No processing history found")

    try:
        with open(PROCESS_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read processing history")

    updated = False
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx].strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("task_id", "")) == task_id:
            row["user_feedback"] = normalized
            row["feedback_updated_at"] = datetime.now(timezone.utc).isoformat()
            lines[idx] = json.dumps(row, ensure_ascii=False) + "\n"
            updated = True
            break

    if not updated:
        raise HTTPException(status_code=404, detail="task_id not found in history")

    try:
        with open(PROCESS_LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to write feedback")

    task = tasks_status.get(task_id)
    if task and isinstance(task, dict):
        task["user_feedback"] = normalized

    return {"ok": True, "task_id": task_id, "feedback": normalized}


@app.post("/api/analyze")
async def analyze_audio_endpoint(file: UploadFile = File(...)):
    allowed_extensions = ('.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus', '.aac', '.wma')
    if not file.filename.lower().endswith(allowed_extensions):
        raise HTTPException(status_code=400, detail="Unsupported file format")

    file_id = str(uuid.uuid4())
    input_path = os.path.join(TEMP_DIR, f"analyze_{file_id}.wav")

    try:
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        analysis = analyze_audio(input_path)
        return {"analysis": analysis}
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)


@app.post("/api/plan")
async def build_plan_endpoint(
    analysis_json: str = Form(...),
    preferred_preset: str = Form("music_balanced")
):
    try:
        analysis = json.loads(analysis_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid analysis_json: {e}")

    plan, source, plan_error = build_processing_plan_with_fallback(
        analysis=analysis,
        preferred_preset=preferred_preset
    )
    return {"processing_plan": plan, "plan_source": source, "plan_error": plan_error}


@app.post("/api/restore")
async def restore_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    preset: str = Form("music_balanced"),
    processing_mode: str = Form("auto"),
    processing_plan_json: str = Form(""),
    plan_source: str = Form("unknown"),
    plan_error: str = Form(""),
    analysis_json: str = Form(""),
    super_res: bool = Form(True),
    val_super_res: float = Form(50.0),
    noise_reduction: bool = Form(True),
    val_noise_reduction: float = Form(60.0),
    punchy_bass: bool = Form(True),
    val_punchy_bass: float = Form(50.0),
    brilliance: bool = Form(True),
    val_brilliance: float = Form(40.0),
    stereo_widener: bool = Form(True),
    val_stereo: float = Form(40.0),
    limiter: bool = Form(True),
    val_limiter: float = Form(80.0),
    highpass: bool = Form(True),
    val_highpass: float = Form(80.0)
):
    allowed_extensions = ('.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus', '.aac', '.wma')
    if not file.filename.lower().endswith(allowed_extensions):
        raise HTTPException(status_code=400, detail="Unsupported file format")

    file_id = str(uuid.uuid4())

    input_path = os.path.join(TEMP_DIR, f"input_{file_id}.wav")
    output_path = os.path.join(TEMP_DIR, f"output_{file_id}.wav")

    with open(input_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    parsed_plan = {}
    if processing_plan_json.strip():
        try:
            parsed_plan = json.loads(processing_plan_json)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid processing_plan_json: {e}")
        parsed_plan = _sanitize_processing_plan(parsed_plan) or {}

    parsed_analysis = {}
    if analysis_json.strip():
        try:
            parsed_analysis = json.loads(analysis_json)
        except json.JSONDecodeError:
            parsed_analysis = {}

    options = {
        "preset": preset,
        "processing_mode": processing_mode,
        "processing_plan": parsed_plan,
        "analysis_hint": parsed_analysis,
        "super_res": super_res,
        "val_super_res": val_super_res,
        "noise_reduction": noise_reduction,
        "val_noise_reduction": val_noise_reduction,
        "punchy_bass": punchy_bass,
        "val_punchy_bass": val_punchy_bass,
        "brilliance": brilliance,
        "val_brilliance": val_brilliance,
        "stereo_widener": stereo_widener,
        "val_stereo": val_stereo,
        "limiter": limiter,
        "val_limiter": val_limiter,
        "highpass": highpass,
        "val_highpass": val_highpass
    }

    tasks_status[file_id] = {"status": "processing"}

    background_tasks.add_task(
        run_processing,
        file_id,
        input_path,
        output_path,
        options,
        {
            "preset": preset,
            "processing_mode": processing_mode,
            "plan_source": plan_source,
            "plan_error": plan_error,
            "analysis": parsed_analysis,
            "filename": file.filename
        }
    )

    return {"task_id": file_id}



# Serve the Frontend HTML/JS/CSS files at the root
# main.py는 backend/ 안에 있으므로 한 단계만 올라가면 test/ 루트
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
frontend_dir = os.path.abspath(frontend_dir)
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

def run_processing(task_id, input_path, output_path, options, trace_meta=None):
    from processing import process_audio

    success, msg, metrics = process_audio(input_path, output_path, options)
    trace_meta = trace_meta or {}

    if success:
        tasks_status[task_id] = {
            "status": "done",
            "output": output_path,
            "metrics": metrics
        }
        _append_process_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "status": "done",
            "filename": trace_meta.get("filename", ""),
            "preset": trace_meta.get("preset", ""),
            "processing_mode": trace_meta.get("processing_mode", ""),
            "plan_source": trace_meta.get("plan_source", ""),
            "plan_error": trace_meta.get("plan_error", ""),
            "analysis": trace_meta.get("analysis", {}),
            "processing_plan": options.get("processing_plan", {}),
            "metrics": metrics
        })
    else:
        tasks_status[task_id] = {
            "status": "error",
            "message": msg
        }
        _append_process_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "status": "error",
            "filename": trace_meta.get("filename", ""),
            "preset": trace_meta.get("preset", ""),
            "processing_mode": trace_meta.get("processing_mode", ""),
            "plan_source": trace_meta.get("plan_source", ""),
            "plan_error": trace_meta.get("plan_error", ""),
            "analysis": trace_meta.get("analysis", {}),
            "processing_plan": options.get("processing_plan", {}),
            "error_message": msg
        })

