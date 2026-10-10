@echo off
setlocal
cd /d "%~dp0" || exit /b 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
set "setupExitCode=%errorlevel%"
if not "%setupExitCode%"=="0" pause
exit /b %setupExitCode%
