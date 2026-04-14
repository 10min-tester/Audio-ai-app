@echo off
echo =========================================
echo Starting Audio Restoration Backend (Server)...
echo It may take some time to install packages on the first run.
echo =========================================

cd backend

echo Installing required Python packages...
python -m pip install -r requirements.txt

echo.
echo Opening the Frontend App in your web browser...
start "" "..\frontend\index.html"

echo.
echo Starting the Backend Server. Close this window to stop the server.
python -m uvicorn main:app

pause
