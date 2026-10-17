@echo off
title CS2 Highlighter
call "%~dp0scripts\check_folder.bat" "%~dp0" || ( pause & exit /b 1 )
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto :run

echo First run: creating the Python environment and downloading radar images ^(1-3 minutes^)...
call scripts\ensure_python.bat || ( pause & exit /b 1 )
"%BASEPY%" -m venv .venv
if not exist ".venv\Scripts\python.exe" ( echo Could not create the Python environment with "%BASEPY%". & pause & exit /b 1 )
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
if errorlevel 1 ( echo Installing dependencies failed. Check your internet connection and run this again. & pause & exit /b 1 )
".venv\Scripts\python.exe" scripts\fetch_radars.py
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
echo.
echo Done. A "CS2 Highlighter" icon was added to your Desktop. Starting the app...

:run
".venv\Scripts\python.exe" run.py
echo.
echo The app has stopped. Press any key to close this window.
pause >nul
