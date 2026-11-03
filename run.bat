@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run offline-setup.bat to create the app environment.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" scripts\restore_critical_assets.py
if errorlevel 1 (
    echo Critical assets could not be restored. Inspect vendor\critical-assets.zip.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" scripts\windows_runtime.py start
if errorlevel 1 echo Some services need attention. Run check-windows.bat and inspect data\runtime\logs.
echo Question UI: http://127.0.0.1:8501
echo Status: status.bat   Stop: stop.bat
pause
