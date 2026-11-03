@echo off
REM Windows RTX 5080 app CUDA setup. OCR retains its separate .venv-ocr.
cd /d "%~dp0"
python --version
if errorlevel 1 (echo NEED Python 3.12 & pause & exit /b 1)
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 (echo VENV FAILED & pause & exit /b 1)
set "RAG_PYTHON=%~dp0.venv\Scripts\python.exe"

echo [1/2] Installing official Windows CUDA 12.8 Torch from the OCR wheel bundle...
"%RAG_PYTHON%" -m pip install --no-index --find-links "%~dp0vendor\wheels-windows-ocr-cu128" --find-links "%~dp0vendor\wheels" "torch==2.10.0+cu128"
if errorlevel 1 (echo TORCH CUDA FAILED & pause & exit /b 1)

echo [2/2] Installing project from vendor\wheels...
"%RAG_PYTHON%" -m pip install --no-index --find-links "%~dp0vendor\wheels" -e "%~dp0"
if errorlevel 1 (echo INSTALL FAILED & pause & exit /b 1)

echo DONE. App CUDA environment installed. Verify GPU compatibility before use.
echo Unlimited-OCR setup: docs\unlimited-ocr-offline.md
pause
