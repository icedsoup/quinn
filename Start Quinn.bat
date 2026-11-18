@echo off
setlocal
cd /d "%~dp0"
if /i "%~1"=="--terminal" goto terminal
for %%D in (.venv venv) do (
    if exist "%%D\Scripts\pythonw.exe" (
        start "" "%%D\Scripts\pythonw.exe" launcher.py --gui
        exit /b
    )
)
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 launcher.py --gui
    exit /b
)
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe launcher.py --gui
    exit /b
)
goto missing

:terminal
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" launcher.py %*
    goto finished
)
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" launcher.py %*
    goto finished
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 launcher.py %*
    goto finished
)
where python >nul 2>nul
if not errorlevel 1 (
    python launcher.py %*
    goto finished
)
:missing
echo Python 3.10 or newer was not found.
exit /b 1
:finished
exit /b
