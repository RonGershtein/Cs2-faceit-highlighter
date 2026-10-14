@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    py -3.11 -m venv .venv 2>nul || py -3.12 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 ( echo Python 3.11+ not found. Install it from python.org and rerun. & pause & exit /b 1 )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
    if errorlevel 1 ( echo pip install failed & pause & exit /b 1 )
    ".venv\Scripts\python.exe" scripts\fetch_radars.py
)
".venv\Scripts\python.exe" run.py
pause
