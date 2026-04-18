"""
v2 entrypoint with storage-aware batch-processing endpoints.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from urllib.parse import unquote

from fastapi import BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

import main as base
import storage_v2 as storage
from processing_v2 import analyze_audio, process_audio

app = base.app
batch_tasks_status: dict[str, dict] = {}


def _move_root_static_mount_to_end():
    routes = list(app.router.routes)
    root_mounts = []
    other_routes = []
    for route in routes:
        path = getattr(route, "path", None)
        if path in ("", "/"):
            root_mounts.append(route)
        else:
            other_routes.append(route)
    if root_mounts:
        app.router.routes[:] = other_routes + root_mounts


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _cleanup_temp_files(max_age_minutes: int = 45):
    temp_dir = os.path.abspath(base.TEMP_DIR)
    os.makedirs(temp_dir, exist_ok=True)
    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_minutes * 60)
    for name in os.listdir(temp_dir):
        if not name.startswith(("batch_input_", "batch_output_", "batch_zip_")):
            continue
        path = os.path.join(temp_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except Exception:
            continue
    storage.cleanup_local_storage(hours=int(os.getenv("LOCAL_STORAGE_TTL_HOURS", "24")))


def _set_feedback_for_task(task_id: str, feedback: str) -> bool:
    if not os.path.exists(base.PROCESS_LOG_PATH):
        return False
    try:
        with open(base.PROCESS_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return False

    updated = False
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx].strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("task_id", "")) == str(task_id):
            row["user_feedback"] = feedback
            row["feedback_updated_at"] = datetime.now(timezone.utc).isoformat()
            lines[idx] = json.dumps(row, ensure_ascii=False) + "\n"
            updated = True
            break
    if not updated:
        return False
    try:
        with open(base.PROCESS_LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        return False
    return True


def _build_options_template(
    preset: str,
    processing_mode: str,
    super_res: bool,
    val_super_res: float,
    noise_reduction: bool,
    val_noise_reduction: float,
    punchy_bass: bool,
    val_punchy_bass: float,
    brilliance: bool,
    val_brilliance: float,
    stereo_widener: bool,
    val_stereo: float,
    limiter: bool,
    val_limiter: float,
    highpass: bool,
    val_highpass: float,
) -> dict:
    return {
        "preset": preset,
        "processing_mode": processing_mode,
        "super_res": bool(super_res),
        "val_super_res": _safe_float(val_super_res, 50.0),
        "noise_reduction": bool(noise_reduction),
        "val_noise_reduction": _safe_float(val_noise_reduction, 60.0),
        "punchy_bass": bool(punchy_bass),
        "val_punchy_bass": _safe_float(val_punchy_bass, 50.0),
        "brilliance": bool(brilliance),
        "val_brilliance": _safe_float(val_brilliance, 40.0),
        "stereo_widener": bool(stereo_widener),
        "val_stereo": _safe_float(val_stereo, 40.0),
        "limiter": bool(limiter),
        "val_limiter": _safe_float(val_limiter, 80.0),
        "highpass": bool(highpass),
        "val_highpass": _safe_float(val_highpass, 80.0),
    }


def _register_batch(batch_id: str, items: list[dict]):
    batch_tasks_status[batch_id] = {
        "status": "processing",
        "stage": "queued",
        "progress": 0,
        "count_total": len(items),
        "items": items,
        "zip_uri": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "storage_mode": storage.get_storage_mode(),
    }


def _make_items_from_refs(batch_id: str, refs: list[dict]) -> list[dict]:
    allowed_extensions = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac", ".wma")
    items = []
    for ref in refs:
        filename = str(ref.get("filename", "") or "")
        input_uri = str(ref.get("input_uri", "") or "")
        if not filename or not input_uri:
            raise HTTPException(status_code=400, detail="Each ref must include filename and input_uri")
        if not filename.lower().endswith(allowed_extensions):
            raise HTTPException(status_code=400, detail=f"Unsupported file format: {filename}")
        task_id = str(uuid.uuid4())
        items.append(
            {
                "index": len(items),
                "task_id": task_id,
                "filename": filename,
                "input_uri": input_uri,
                "output_uri": "",
                "status": "queued",
                "stage": "queued",
                "message": "",
            }
        )
    if not items:
        raise HTTPException(status_code=400, detail="No items provided")
    if len(items) > 20:
        raise HTTPException(status_code=400, detail="Batch limit exceeded (max 20 files)")
    return items


def _batch_run_processing(batch_id: str, items: list[dict], options_template: dict, preset: str, processing_mode: str):
    batch = batch_tasks_status.get(batch_id)
    if not batch:
        return

    total = max(len(items), 1)
    completed = 0
    any_error = False
    batch["stage"] = "running"

    temp_files_to_cleanup = []
    zip_temp_path = os.path.join(base.TEMP_DIR, f"batch_zip_{batch_id}.zip")
    temp_files_to_cleanup.append(zip_temp_path)
    os.makedirs(base.TEMP_DIR, exist_ok=True)

    with zipfile.ZipFile(zip_temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, item in enumerate(items, start=1):
            item["status"] = "processing"
            item["stage"] = "downloading"
            batch["progress"] = int((completed / total) * 100)
            batch["updated_at"] = datetime.now(timezone.utc).isoformat()

            input_temp = os.path.join(base.TEMP_DIR, f"batch_input_{batch_id}_{item['task_id']}.wav")
            output_temp = os.path.join(base.TEMP_DIR, f"batch_output_{batch_id}_{item['task_id']}.wav")
            temp_files_to_cleanup.extend([input_temp, output_temp])

            try:
                storage.read_to_local_temp(item["input_uri"], input_temp)
                item["stage"] = "analyzing"
                analysis = analyze_audio(input_temp)
                processing_plan, plan_source, plan_error = base.build_processing_plan_with_fallback(
                    analysis=analysis,
                    preferred_preset=preset,
                )

                options = dict(options_template)
                options["analysis_hint"] = analysis
                options["processing_plan"] = processing_plan or {}

                item["stage"] = "processing"
                ok, msg, metrics = process_audio(input_temp, output_temp, options)
                if not ok:
                    any_error = True
                    item["status"] = "error"
                    item["message"] = str(msg)
                    item["stage"] = "failed"
                else:
                    item["status"] = "done"
                    item["metrics"] = metrics
                    item["plan_source"] = plan_source
                    item["plan_error"] = plan_error
                    base_name, _ = os.path.splitext(item["filename"])
                    stored_name = f"{base_name}_restored.wav"
                    item["output_uri"] = storage.upload_from_local_temp(output_temp, stored_name, "outputs")
                    zf.write(output_temp, arcname=f"{idx:02d}_{stored_name}")
                    item["stage"] = "done"

                base._append_process_log(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "task_id": item["task_id"],
                        "status": item["status"],
                        "filename": item["filename"],
                        "preset": preset,
                        "processing_mode": processing_mode,
                        "plan_source": item.get("plan_source", ""),
                        "plan_error": item.get("plan_error", ""),
                        "analysis": analysis,
                        "processing_plan": options.get("processing_plan", {}),
                        "metrics": item.get("metrics", {}),
                        "error_message": item.get("message", ""),
                    }
                )
            except Exception as e:
                any_error = True
                item["status"] = "error"
                item["stage"] = "failed"
                item["message"] = str(e)

            completed += 1
            batch["progress"] = int((completed / total) * 100)
            batch["updated_at"] = datetime.now(timezone.utc).isoformat()

    if any(item.get("status") == "done" for item in items):
        batch["zip_uri"] = storage.upload_from_local_temp(
            zip_temp_path,
            f"restored_batch_{batch_id}.zip",
            "archives",
        )

    done_count = sum(1 for item in items if item.get("status") == "done")
    if done_count == 0 and any_error:
        batch["status"] = "error"
    else:
        batch["status"] = "done_with_errors" if any_error else "done"

    batch["stage"] = "done"
    batch["updated_at"] = datetime.now(timezone.utc).isoformat()

    for path in temp_files_to_cleanup:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            continue


@app.post("/api/v2/storage/init-upload")
async def init_upload_v2(filename: str = Form(...), content_type: str = Form("application/octet-stream")):
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    _cleanup_temp_files()
    return storage.create_upload_session(filename=filename, content_type=content_type)


@app.put("/api/v2/storage/upload/{file_id}")
async def upload_local_object_v2(file_id: str, request: Request, key: str):
    decoded_key = unquote(key)
    if file_id not in decoded_key:
        raise HTTPException(status_code=400, detail="Invalid upload key")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty upload body")
    storage.save_upload_bytes_local(f"local://{decoded_key}", body)
    return {"ok": True, "file_id": file_id}


@app.get("/api/v2/storage/object/{object_path:path}")
def get_local_object_v2(object_path: str):
    uri = f"local://{object_path}"
    try:
        path = storage.resolve_local_path_from_uri(uri)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid object path")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Object not found")
    return FileResponse(path)


@app.post("/api/v2/restore-batch-by-ref")
async def restore_batch_by_ref_v2(
    background_tasks: BackgroundTasks,
    refs_json: str = Form(...),
    preset: str = Form("music_balanced"),
    processing_mode: str = Form("full"),
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
    val_highpass: float = Form(80.0),
):
    try:
        refs = json.loads(refs_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid refs_json: {e}")
    if not isinstance(refs, list):
        raise HTTPException(status_code=400, detail="refs_json must be a list")

    batch_id = str(uuid.uuid4())
    items = _make_items_from_refs(batch_id, refs)
    options_template = _build_options_template(
        preset,
        processing_mode,
        super_res,
        val_super_res,
        noise_reduction,
        val_noise_reduction,
        punchy_bass,
        val_punchy_bass,
        brilliance,
        val_brilliance,
        stereo_widener,
        val_stereo,
        limiter,
        val_limiter,
        highpass,
        val_highpass,
    )
    _register_batch(batch_id, items)
    background_tasks.add_task(_batch_run_processing, batch_id, items, options_template, preset, processing_mode)
    return {"batch_id": batch_id, "storage_mode": storage.get_storage_mode()}


@app.post("/api/v2/restore-batch")
async def restore_batch_v2_compat(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    preset: str = Form("music_balanced"),
    processing_mode: str = Form("full"),
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
    val_highpass: float = Form(80.0),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    refs = []
    for upl in files:
        filename = upl.filename or ""
        sess = storage.create_upload_session(filename=filename, content_type=upl.content_type or "application/octet-stream")
        payload = await upl.read()
        if sess["storage_mode"] == "local":
            storage.save_upload_bytes_local(sess["input_uri"], payload)
        else:
            import urllib.request

            req = urllib.request.Request(
                sess["upload"]["url"],
                data=payload,
                method="PUT",
                headers=sess["upload"].get("headers", {}),
            )
            with urllib.request.urlopen(req, timeout=30):
                pass
        refs.append({"filename": filename, "input_uri": sess["input_uri"]})

    batch_id = str(uuid.uuid4())
    items = _make_items_from_refs(batch_id, refs)
    options_template = _build_options_template(
        preset,
        processing_mode,
        super_res,
        val_super_res,
        noise_reduction,
        val_noise_reduction,
        punchy_bass,
        val_punchy_bass,
        brilliance,
        val_brilliance,
        stereo_widener,
        val_stereo,
        limiter,
        val_limiter,
        highpass,
        val_highpass,
    )
    _register_batch(batch_id, items)
    background_tasks.add_task(_batch_run_processing, batch_id, items, options_template, preset, processing_mode)
    return {"batch_id": batch_id, "storage_mode": storage.get_storage_mode(), "compat_upload": True}


@app.get("/api/v2/status-batch/{batch_id}")
def status_batch_v2(batch_id: str):
    batch = batch_tasks_status.get(batch_id)
    if not batch:
        return {"status": "not_found"}

    items = []
    for item in batch.get("items", []):
        items.append(
            {
                "index": item.get("index"),
                "task_id": item.get("task_id"),
                "filename": item.get("filename"),
                "status": item.get("status"),
                "stage": item.get("stage"),
                "message": item.get("message", ""),
                "metrics": item.get("metrics", {}),
                "plan_source": item.get("plan_source", ""),
                "plan_error": item.get("plan_error", ""),
            }
        )

    plan_source_counts = {}
    for item in items:
        source = str(item.get("plan_source", "") or "unknown")
        plan_source_counts[source] = plan_source_counts.get(source, 0) + 1

    return {
        "status": batch.get("status"),
        "stage": batch.get("stage"),
        "progress": batch.get("progress", 0),
        "count_total": batch.get("count_total", 0),
        "count_done": sum(1 for i in items if i.get("status") == "done"),
        "count_error": sum(1 for i in items if i.get("status") == "error"),
        "count_processing": sum(1 for i in items if i.get("status") == "processing"),
        "count_queued": sum(1 for i in items if i.get("status") == "queued"),
        "created_at": batch.get("created_at"),
        "updated_at": batch.get("updated_at"),
        "storage_mode": batch.get("storage_mode", storage.get_storage_mode()),
        "plan_source_counts": plan_source_counts,
        "items": items,
    }


@app.get("/api/v2/download-batch/{batch_id}")
def download_batch_v2(batch_id: str):
    batch = batch_tasks_status.get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    if batch.get("status") == "error":
        raise HTTPException(status_code=400, detail="Batch failed. No output archive is available.")
    if batch.get("status") not in ("done", "done_with_errors"):
        raise HTTPException(status_code=400, detail="Batch is not ready")

    zip_uri = str(batch.get("zip_uri", "") or "")
    if not zip_uri:
        raise HTTPException(status_code=404, detail="Batch zip not found")
    url = storage.create_download_url(zip_uri, filename=f"restored_batch_{batch_id}.zip")
    if url.startswith("/api/v2/storage/object/"):
        rel = url.split("/api/v2/storage/object/", 1)[1]
        return get_local_object_v2(rel)
    return RedirectResponse(url=url, status_code=307)


@app.get("/api/v2/download-item/{batch_id}/{item_index}")
def download_batch_item_v2(batch_id: str, item_index: int):
    batch = batch_tasks_status.get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    items = batch.get("items", [])
    if item_index < 0 or item_index >= len(items):
        raise HTTPException(status_code=404, detail="Item not found")

    item = items[item_index]
    if item.get("status") != "done":
        raise HTTPException(status_code=400, detail="Item is not ready")
    output_uri = str(item.get("output_uri", "") or "")
    if not output_uri:
        raise HTTPException(status_code=404, detail="Output file not found")

    base_name, _ = os.path.splitext(item.get("filename", f"item_{item_index+1}"))
    out_filename = f"{base_name}_restored.wav"
    url = storage.create_download_url(output_uri, filename=out_filename)
    if url.startswith("/api/v2/storage/object/"):
        rel = url.split("/api/v2/storage/object/", 1)[1]
        return get_local_object_v2(rel)
    return RedirectResponse(url=url, status_code=307)


@app.post("/api/v2/feedback")
async def submit_feedback_v2(batch_id: str = Form(...), feedback: str = Form(...), item_index: int = Form(-1)):
    normalized = feedback.strip().lower()
    if normalized not in ("good", "neutral", "bad"):
        raise HTTPException(status_code=400, detail="feedback must be one of: good, neutral, bad")
    batch = batch_tasks_status.get(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    items = batch.get("items", [])
    target_items = [items[item_index]] if item_index >= 0 and item_index < len(items) else []
    if item_index < 0:
        target_items = [item for item in items if item.get("status") == "done"]
    if not target_items:
        raise HTTPException(status_code=400, detail="No completed items to record feedback")

    updated_count = 0
    for item in target_items:
        task_id = str(item.get("task_id", ""))
        if task_id and _set_feedback_for_task(task_id, normalized):
            item["user_feedback"] = normalized
            updated_count += 1
    if updated_count == 0:
        raise HTTPException(status_code=404, detail="No matching history rows were found")

    return {"ok": True, "batch_id": batch_id, "item_index": item_index, "feedback": normalized, "updated_count": updated_count}


@app.get("/api/v2/insights")
async def get_quality_insights_v2(limit: int = 200):
    safe_limit = int(max(20, min(limit, 2000)))
    rows = base._read_recent_logs(limit=safe_limit)
    return {"summary": base._summarize_quality(rows)}


_cleanup_temp_files()
_move_root_static_mount_to_end()

