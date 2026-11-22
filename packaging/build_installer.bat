@echo off
REM ==========================================================================================
REM  Build ARES-Battery-Guardian-V2-Beta-Setup.exe
REM
REM    packaging\build_installer.bat            build + verify + installer (no install test)
REM    packaging\build_installer.bat test       ... and run the install/update/uninstall test
REM
REM  Uses the project's existing .venv (Python 3.14) and checks that it matches requirements.txt
REM  exactly; nothing is installed or upgraded. Writes ONLY to build_v2beta\, dist_v2beta\,
REM  installer_output\ and packaging\logs\. The existing build\, dist\, release\, data\, logs\
REM  and .venv\ contents are not modified, and user data in %LOCALAPPDATA% is never touched.
REM
REM  Needs Inno Setup 6 (ISCC.exe) - https://jrsoftware.org/isinfo.php
REM ==========================================================================================
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"
set "LOGS=%ROOT%\packaging\logs"
if not exist "%LOGS%" mkdir "%LOGS%"
set "PY=%ROOT%\.venv\Scripts\python.exe"

echo [1/6] Checking the build environment...
if not exist "%PY%" (
    echo .venv not found. Create it first:  py -3.14 -m venv .venv ^&^& .venv\Scripts\python -m pip install -r requirements.txt
    exit /b 1
)
"%PY%" packaging\check_env.py > "%LOGS%\01_environment.log" 2>&1
type "%LOGS%\01_environment.log"
if errorlevel 1 goto :error

echo [2/6] Running the unit tests...
set QT_QPA_PLATFORM=offscreen
"%PY%" -m unittest discover -s tests -v > "%LOGS%\02_unit_tests.log" 2>&1
set "RC=%errorlevel%"
set QT_QPA_PLATFORM=
findstr /R /C:"^Ran " /C:"^OK" /C:"^FAILED" "%LOGS%\02_unit_tests.log"
if not "%RC%"=="0" goto :error

echo [3/6] Building the program with PyInstaller...
"%PY%" -m PyInstaller --noconfirm --clean --distpath dist_v2beta --workpath build_v2beta ^
    packaging\ARES-Battery-Guardian-V2-Beta.spec > "%LOGS%\03_pyinstaller.log" 2>&1
if errorlevel 1 goto :error
if not exist "dist_v2beta\ARES-Battery-Guardian\ARES-Battery-Guardian.exe" goto :error
echo    dist_v2beta\ARES-Battery-Guardian\ARES-Battery-Guardian.exe

echo [4/6] Checking the packaged program...
"%PY%" packaging\verify_package.py --log "%LOGS%\04_verify_package.log"
if errorlevel 1 goto :error

echo [5/6] Building the installer with Inno Setup...
set "ISCC="
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do (
    if exist %%P set "ISCC=%%~P"
)
if not defined ISCC (
    echo Inno Setup 6 was not found. Install it from https://jrsoftware.org/isinfo.php and run this again.
    exit /b 1
)
"%ISCC%" packaging\ARES-Battery-Guardian-V2-Beta.iss > "%LOGS%\05_inno_setup.log" 2>&1
if errorlevel 1 goto :error
set "SETUP=%ROOT%\installer_output\ARES-Battery-Guardian-V2-Beta-Setup.exe"
certutil -hashfile "%SETUP%" SHA256 > "%LOGS%\setup_sha256.txt"
type "%LOGS%\setup_sha256.txt"

if /i "%~1"=="test" (
    echo [6/6] Smart Audio Override on the real audio device ^(no sound; about 2 s at 100%% volume, then restored^)...
    "%PY%" packaging\audio_hw_check.py --log "%LOGS%\07_audio_hw_check.log"
    if errorlevel 1 goto :error
    echo [6/6] Testing install, update, uninstall and reinstall...
    "%PY%" packaging\test_installer.py --setup "%SETUP%" --log "%LOGS%\06_test_installer.log"
    if errorlevel 1 goto :error
) else (
    echo [6/6] Install test skipped. Run "packaging\build_installer.bat test" to include it.
)
echo.
echo Done: %SETUP%
echo Logs: %LOGS%
exit /b 0

:error
echo.
echo Build stopped with an error. See the logs in %LOGS%
exit /b 1
