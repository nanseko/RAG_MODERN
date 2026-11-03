@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run offline-setup.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" scripts\windows_runtime.py doctor
pause
