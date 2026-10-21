@echo off
rem Sets UV (for the caller) to tools\uv\uv.exe, downloading uv from GitHub the first time (about 16 MB).
rem uv installs its own Python 3.12 and the locked packages (uv.lock); Python and the download cache live in tools\
rem too, so nothing is installed system-wide and deleting the folder removes everything.
rem Keep UV_VERSION in step with launcher\Launcher.cs.
set "UV_VERSION=0.13.0"
for %%I in ("%~dp0..") do set "APPROOT=%%~fI"
set "UV=%APPROOT%\tools\uv\uv.exe"
set "UV_PYTHON_INSTALL_DIR=%APPROOT%\tools\python"
set "UV_CACHE_DIR=%APPROOT%\tools\uv-cache"
set "UV_PYTHON_PREFERENCE=only-managed"
if exist "%UV%" exit /b 0
echo Downloading uv (installs Python and the app's components, about 16 MB)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; $z=Join-Path $env:TEMP 'cs2h-uv.zip'; Invoke-WebRequest -UseBasicParsing 'https://github.com/astral-sh/uv/releases/download/%UV_VERSION%/uv-x86_64-pc-windows-msvc.zip' -OutFile $z; Expand-Archive -Force $z '%APPROOT%\tools\uv'; Remove-Item $z"
if not exist "%UV%" ( echo Could not download uv. Check your internet connection and run this again. & exit /b 1 )
exit /b 0
