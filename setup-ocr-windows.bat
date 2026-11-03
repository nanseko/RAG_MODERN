@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0vendor\runtime\python\python.exe"
if not exist "%PYTHON%" set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Native Windows Python 3.12 x64 is required. Run the main offline installer first.
  exit /b 1
)
"%PYTHON%" scripts\prepare_windows_ocr.py --install
if errorlevel 1 exit /b 1
.venv-ocr\Scripts\python.exe scripts\prepare_windows_ocr.py --check-runtime
exit /b %errorlevel%
