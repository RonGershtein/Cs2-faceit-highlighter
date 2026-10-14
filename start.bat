@echo off
title CS2 Highlighter
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: creating the Python environment and downloading radar images ^(1-3 minutes^)...
    py -3.12 -m venv .venv 2>nul || py -3.11 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" ( echo Python 3.11+ not found. Install it from python.org ^(tick "Add python.exe to PATH"^) and run this again. & pause & exit /b 1 )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
    if errorlevel 1 ( echo Installing dependencies failed. Check your internet connection and run this again. & pause & exit /b 1 )
    ".venv\Scripts\python.exe" scripts\fetch_radars.py
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
    echo.
    echo Done. A "CS2 Highlighter" icon was added to your Desktop. Starting the app...
)
".venv\Scripts\python.exe" run.py
echo.
echo The app has stopped. Press any key to close this window.
pause >nul
