from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import shutil
import os
import uuid
from processing import process_audio

app = FastAPI(title="Audio Restoration API")

# Setup CORS for Frontend connectivity
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = "temp_audio"
os.makedirs(TEMP_DIR, exist_ok=True)

@app.get("/")
def read_root():
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
    if not file.filename.endswith(('.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus', '.aac', '.wma')):
        raise HTTPException(status_code=400, detail="Unsupported file format")

    file_id = str(uuid.uuid4())
    input_filename = f"input_{file_id}_{file.filename}"
    output_filename = f"output_{file_id}.wav"
    
    input_path = os.path.join(TEMP_DIR, input_filename)
    output_path = os.path.join(TEMP_DIR, output_filename)

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

    # Process audio with selected options
    success, msg, metrics = process_audio(input_path, output_path, options)
    
    if not success:
        raise HTTPException(status_code=500, detail=f"Processing failed: {msg}")

    headers = {
        "X-Analysis-LUFS": str(metrics.get("LUFS", 0)),
        "X-Analysis-Crest": str(metrics.get("Crest_dB", 0)),
        "X-Analysis-Phase": str(metrics.get("Phase_Corr", 0)),
        "Access-Control-Expose-Headers": "X-Analysis-LUFS, X-Analysis-Crest, X-Analysis-Phase"
    }

    return FileResponse(
        path=output_path, 
        filename=f"restored_{file.filename}.wav",
        media_type="audio/wav",
        headers=headers
    )

# Serve the Frontend HTML/JS/CSS files at the root
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
