@echo off
REM Offline setup (run once, no internet): torch CPU + project from vendor\wheels
cd /d "%~dp0"

echo [1/3] Checking Python...
python --version
if errorlevel 1 (echo NEED Python 3.12 & pause & exit /b 1)
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 (echo VENV FAILED & pause & exit /b 1)
set "RAG_PYTHON=%~dp0.venv\Scripts\python.exe"

echo [2/3] Installing torch CPU from vendor\wheels (offline)...
"%RAG_PYTHON%" -m pip install --no-index --find-links "%~dp0vendor\wheels" "torch==2.14.1+cpu"
if errorlevel 1 (echo TORCH INSTALL FAILED & pause & exit /b 1)

echo [3/3] Installing project from vendor\wheels (offline)...
"%RAG_PYTHON%" -m pip install --no-index --find-links "%~dp0vendor\wheels" -e "%~dp0"
if errorlevel 1 (echo OFFLINE INSTALL FAILED & pause & exit /b 1)

echo DONE. Next: run run.bat
pause
