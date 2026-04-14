from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import shutil
import os
import uuid
from processing import process_audio

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

@app.get("/api/health")
async def health_check():
    return {"message": "Audio Restoration Backend Running"}

@app.post("/api/restore")
async def restore_audio(
    file: UploadFile = File(...),
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
    val_limiter: float = Form(80.0)
):
    allowed_extensions = ('.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus', '.aac', '.wma')
    if not file.filename.lower().endswith(allowed_extensions):
        raise HTTPException(status_code=400, detail="Unsupported file format")

    file_id = str(uuid.uuid4())
    input_filename = f"input_{file_id}_{file.filename}"
    output_filename = f"output_{file_id}.wav"
    
    input_path = os.path.join(TEMP_DIR, input_filename)
    output_path = os.path.join(TEMP_DIR, output_filename)

    try:
        # Save uploaded file
        with open(input_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        options = {
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
            "val_limiter": val_limiter
        }

        success, msg, metrics = process_audio(input_path, output_path, options)
        
        if not success:
            raise HTTPException(status_code=500, detail=f"Processing failed: {msg}")

        headers = {
            "X-Analysis-LUFS": str(metrics.get("LUFS", 0)),
            "X-Analysis-Crest": str(metrics.get("Crest_dB", 0)),
            "X-Analysis-Phase": str(metrics.get("Phase_Corr", 0)),
        }

        # [수정] background_tasks로 임시 파일 정리 (응답 전송 후 삭제)
        from starlette.background import BackgroundTask

        def cleanup():
            for path in [input_path]:  # output은 FileResponse가 전송 후 삭제
                if os.path.exists(path):
                    os.remove(path)

        return FileResponse(
            path=output_path,
            filename=f"restored_{file.filename}.wav",
            media_type="audio/wav",
            headers=headers,
            background=BackgroundTask(cleanup)
        )

    except HTTPException:
        raise
    except Exception as e:
        # [수정] 예외 발생 시에도 임시 파일 정리
        for path in [input_path, output_path]:
            if os.path.exists(path):
                os.remove(path)
        raise HTTPException(status_code=500, detail=str(e))

# Serve the Frontend HTML/JS/CSS files at the root
# main.py는 backend/ 안에 있으므로 한 단계만 올라가면 test/ 루트
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
frontend_dir = os.path.abspath(frontend_dir)
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
