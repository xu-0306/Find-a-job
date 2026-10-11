@echo off
setlocal
cd /d "%~dp0" || exit /b 1
if not exist ".venv\Scripts\python.exe" (
    echo Create this checkout's environment first:
    echo   py -3.13 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.lock
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" scripts\dev.py %*
exit /b %errorlevel%
