@echo off
REM Read-only hardware check. Right-click > Run as administrator so Dell Command | Configure can read the BIOS.
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python.exe) else (set PY=py -3.12)
%PY% main.py --diagnose
echo.
pause
