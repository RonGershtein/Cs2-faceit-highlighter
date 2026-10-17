@echo off
rem Refuses to run from a network/WSL path (\\wsl.localhost\..., \\server\share), because
rem cmd.exe cannot use it as a working directory and a Windows venv would land on the Linux disk.
set "ROOT=%~1"
if not "%ROOT:~0,2%"=="\\" exit /b 0
echo.
echo This folder is on a network path:  %ROOT%
echo Windows cannot run the app from there ^(for example from inside WSL^).
echo Copy the folder to a normal Windows location such as C:\cs2-highlighter, or clone it there:
echo     git clone https://github.com/RonGershtein/Cs2-faceit-highlighter C:\cs2-highlighter
echo then run start.bat from that copy.
exit /b 1
