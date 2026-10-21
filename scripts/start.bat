@echo off
title CS2 Highlighter
rem Same as "CS2 Highlighter.exe", but in a console window (handy for seeing errors live, or when the exe is blocked).
rem Lives in scripts\; everything runs from the app folder one level up.
for %%I in ("%~dp0..") do set "APPROOT=%%~fI"
call "%~dp0check_folder.bat" "%APPROOT%" || ( pause & exit /b 1 )
cd /d "%APPROOT%"
call scripts\ensure_uv.bat || ( pause & exit /b 1 )
set "FIRST="
if not exist ".venv\Scripts\python.exe" set "FIRST=1"
if defined FIRST echo First run: installing Python and the app's components ^(1-2 minutes^)...
"%UV%" sync --frozen --no-dev
if errorlevel 1 ( echo Installing failed. Check your internet connection and run this again. & pause & exit /b 1 )
".venv\Scripts\python.exe" scripts\fetch_radars.py
if defined FIRST (
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
    echo.
    echo Done. A "CS2 Highlighter" icon was added to your Desktop. Starting the app...
)
".venv\Scripts\python.exe" run.py
echo.
echo The app has stopped. Press any key to close this window.
pause >nul
