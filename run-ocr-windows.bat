@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv-ocr\Scripts\python.exe" (
  echo OCR environment is missing. Run setup-ocr-windows.bat first.
  exit /b 1
)
.venv-ocr\Scripts\python.exe scripts\prepare_windows_ocr.py --serve
exit /b %errorlevel%
