@echo off
echo =========================================
echo Starting Audio Restoration V2 (separate)
echo =========================================

cd /d "%~dp0backend"

echo Opening V2 UI in browser...
start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:8000/index_v2.html"

echo.
echo Starting V2 backend server (main_v2:app)...
python -m uvicorn main_v2:app --host 0.0.0.0 --port 8000

pause
