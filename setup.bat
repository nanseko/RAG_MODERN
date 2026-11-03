@echo off
cd /d "%~dp0"
python --version
if errorlevel 1 (echo NEED Python 3.12 & pause & exit /b 1)
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 (echo VENV FAILED & pause & exit /b 1)
set "RAG_PYTHON=%~dp0.venv\Scripts\python.exe"
"%RAG_PYTHON%" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 (echo TORCH INSTALL FAILED & pause & exit /b 1)
"%RAG_PYTHON%" -m pip install -e .
if errorlevel 1 (echo INSTALL FAILED & pause & exit /b 1)
echo DONE. Next: run run.bat
pause
