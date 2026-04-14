from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import shutil
import os
import uuid
from processing import process_audio
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

@app.post("/api/restore")
async def restore_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    preset: str = Form("music_balanced"),
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

    options = {
        "preset": preset,
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
        options
    )

    return {"task_id": file_id}



# Serve the Frontend HTML/JS/CSS files at the root
# main.py는 backend/ 안에 있으므로 한 단계만 올라가면 test/ 루트
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
frontend_dir = os.path.abspath(frontend_dir)
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

def run_processing(task_id, input_path, output_path, options):
    from processing import process_audio

    success, msg, metrics = process_audio(input_path, output_path, options)

    if success:
        tasks_status[task_id] = {
            "status": "done",
            "output": output_path,
            "metrics": metrics
        }
    else:
        tasks_status[task_id] = {
            "status": "error",
            "message": msg
        }

