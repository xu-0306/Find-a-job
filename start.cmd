@echo off
setlocal
cd /d "%~dp0" || exit /b 1

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv\Scripts\python.exe was not found. Run setup.cmd first.
    pause
    exit /b 1
)

echo Starting Job Companion...
echo Press Ctrl+C to stop the server. Keep this window open while using the app.
echo.
"%~dp0.venv\Scripts\python.exe" -m app serve --restart %*
set "startupExitCode=%errorlevel%"
if not "%startupExitCode%"=="0" pause
exit /b %startupExitCode%
