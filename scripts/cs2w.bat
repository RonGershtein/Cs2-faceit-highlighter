@echo off
rem CS2 FACEIT Highlighter — Windows command wrapper (same commands as the Makefile).
rem   scripts\cs2w install | run | tools | doctor | test | parse <demo> | update | clean
rem Lives in scripts\; everything runs from the app folder one level up.
setlocal
for %%I in ("%~dp0..") do set "APPROOT=%%~fI"
call "%~dp0check_folder.bat" "%APPROOT%" || exit /b 1
cd /d "%APPROOT%"
set PYEXE=.venv\Scripts\python.exe
set CMD=%1
if "%CMD%"=="" set CMD=help

if "%CMD%"=="help" (
    echo scripts\cs2w install  - install Python + the locked dependencies with uv, download radars
    echo scripts\cs2w run      - start the app at http://localhost:8000
    echo scripts\cs2w tools    - download HLAE + FFmpeg for recording
    echo scripts\cs2w doctor   - check Python, dependencies, CS2 path, tools, config
    echo scripts\cs2w test     - run unit tests
    echo scripts\cs2w parse ^<demo.dem.zst^>  - parse one demo from the command line
    echo scripts\cs2w update   - git pull + reinstall dependencies
    echo scripts\cs2w shortcut - put a "CS2 Highlighter" icon on the Desktop
    echo scripts\cs2w clean    - remove the Python env ^(keeps your data; tools\python and tools\uv stay^)
    goto :eof
)

if "%CMD%"=="clean" (
    if exist .venv rmdir /s /q .venv
    if exist .pytest_cache rmdir /s /q .pytest_cache
    echo cleaned
    goto :eof
)

if "%CMD%"=="update" (
    git pull --ff-only || (echo git pull failed ^(is this a git clone?^) & exit /b 1)
    call "%~f0" install
    goto :eof
)

call scripts\ensure_uv.bat || exit /b 1
if "%CMD%"=="install" (
    "%UV%" sync --frozen || (echo installing failed & exit /b 1)
    "%PYEXE%" scripts\fetch_radars.py
    echo installed. next: scripts\cs2w run
    goto :eof
)
if not exist "%PYEXE%" (
    echo Python environment missing, running install first...
    "%UV%" sync --frozen || (echo installing failed & exit /b 1)
    "%PYEXE%" scripts\fetch_radars.py
)
if "%CMD%"=="run"    ( "%PYEXE%" run.py & goto :eof )
if "%CMD%"=="tools"  ( "%PYEXE%" scripts\fetch_tools.py & goto :eof )
if "%CMD%"=="doctor" ( "%PYEXE%" scripts\doctor.py & goto :eof )
if "%CMD%"=="test"   ( "%UV%" run --frozen pytest -q tests\test_highlights.py tests\test_record.py & goto :eof )
if "%CMD%"=="parse"  ( "%PYEXE%" -m backend.ingest %2 & goto :eof )
if "%CMD%"=="shortcut" ( powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1 & goto :eof )
echo unknown command "%CMD%". Run scripts\cs2w help.
exit /b 1
