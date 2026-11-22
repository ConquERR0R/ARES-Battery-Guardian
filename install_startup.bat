@echo off
REM OPTIONAL: upgrade ARES startup to an elevated Task Scheduler task.
REM Normal startup needs no admin (Settings > Startup uses your user's Run key).
REM Only needed if ARES should re-apply the Dell BIOS charge limit at sign-in.
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
if exist "%~dp0ARES-Battery-Guardian.exe" (
    "%~dp0ARES-Battery-Guardian.exe" --install-startup
) else if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" main.py --install-startup
) else (
    py -3.12 main.py --install-startup
)
echo.
echo Check Task Scheduler for the task "ARES Battery Guardian".
pause
