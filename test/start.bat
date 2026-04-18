@echo off
cd /d "%~dp0backend"
python -m uvicorn main_v2:app --host 0.0.0.0 --port 8000
pause