@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" launcher.py
    goto finished
)
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" launcher.py
    goto finished
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 launcher.py
    goto finished
)
where python >nul 2>nul
if not errorlevel 1 (
    python launcher.py
    goto finished
)
echo Python 3.10 or newer was not found.
pause
exit /b 1
:finished
if errorlevel 1 pause
endlocal
