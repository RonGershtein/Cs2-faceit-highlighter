@echo off
rem Sets BASEPY (for the caller) to a Python 3.11+ interpreter, offering to install Python 3.12
rem with winget when none is found. Exits 1 when Python is still missing.
set "BASEPY="
for /f "delims=" %%P in ('call "%~dp0find_python.bat"') do set "BASEPY=%%P"
if defined BASEPY exit /b 0

echo.
echo Python 3.11+ was not found on this PC.
set "WINGET=winget"
where winget >nul 2>&1 || set "WINGET=%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe"
if not exist "%WINGET%" (
    echo Install Python 3.12 from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
    exit /b 1
)
choice /c YN /n /m "Install Python 3.12 now with winget (Microsoft's package manager)? [Y/N] "
if errorlevel 2 (
    echo Install Python 3.12 from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
    exit /b 1
)
"%WINGET%" install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
for /f "delims=" %%P in ('call "%~dp0find_python.bat"') do set "BASEPY=%%P"
if defined BASEPY exit /b 0
echo Python was installed but this window cannot see it yet. Close this window and run this again.
exit /b 1
