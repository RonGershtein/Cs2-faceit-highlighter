@echo off
rem Prints the path of a Python 3.11+ interpreter and exits 0; prints nothing and exits 1 if none.
rem Looks at the py launcher and PATH first, then at the usual install folders, because the
rem python.org installer only touches PATH when "Add python.exe to PATH" was ticked.
setlocal
for %%V in (3.12 3.11 3.13) do (
    py -%%V -c "import sys" >nul 2>&1 && ( py -%%V -c "import sys; print(sys.executable)" & exit /b 0 )
)
for %%C in (python3.12 python3.11 python3.13 python3 python) do (
    %%C -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && ( %%C -c "import sys; print(sys.executable)" & exit /b 0 )
)
for %%V in (312 311 313) do (
    for %%D in ("%LOCALAPPDATA%\Programs\Python\Python%%V" "%ProgramFiles%\Python%%V" "C:\Python%%V") do (
        if exist "%%~D\python.exe" ( echo %%~D\python.exe & exit /b 0 )
    )
)
exit /b 1
