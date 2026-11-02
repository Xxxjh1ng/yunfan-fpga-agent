@echo off
rem YunFan visual console launcher.
rem Double-click this file: it starts the local server and opens the page.
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] virtualenv not found: "%PY%"
    echo See the deployment guide under docs\ for setup steps.
    pause
    exit /b 1
)
echo Starting YunFan console at http://127.0.0.1:8765 ...
start "" http://127.0.0.1:8765
"%PY%" gui\server.py --port 8765
