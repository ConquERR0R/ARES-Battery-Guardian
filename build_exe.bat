@echo off
REM Build ARES Battery Guardian into dist\ARES-Battery-Guardian\ARES-Battery-Guardian.exe
cd /d "%~dp0"
if not exist .venv (
    py -3.14 -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt || goto :error
python -m unittest discover -s tests || goto :error
pyinstaller --noconfirm --clean ARES-Battery-Guardian.spec || goto :error
echo.
echo Build complete: dist\ARES-Battery-Guardian\ARES-Battery-Guardian.exe
goto :eof
:error
echo Build failed with error %errorlevel%.
exit /b %errorlevel%
