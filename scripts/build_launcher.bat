@echo off
rem Builds "CS2 Highlighter.exe" from launcher\Launcher.cs with the C# compiler that ships with Windows
rem (.NET Framework 4.x), so building needs nothing extra and the exe runs on any Windows 10/11.
setlocal
cd /d "%~dp0.."
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" ( echo The .NET Framework 4 C# compiler was not found. & exit /b 1 )
"%CSC%" /nologo /target:winexe /optimize+ /platform:anycpu /win32icon:assets\icon.ico ^
    /r:System.Management.dll /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll ^
    "/out:CS2 Highlighter.exe" launcher\Launcher.cs || exit /b 1
echo built "CS2 Highlighter.exe"
