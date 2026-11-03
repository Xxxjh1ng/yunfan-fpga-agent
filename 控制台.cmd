@echo off
rem YunFan visual console launcher.
rem Double-click to start the local server and open the page in a browser.
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto run

rem No virtualenv (e.g. fresh unzip): fall back to system Python on PATH.
set "PY=python"
python --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.8+ from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH", then run this file again.
    pause
    exit /b 1
)

:run
echo Starting YunFan console at http://127.0.0.1:8765 ...
echo Close this window to stop the server.
"%PY%" -m yunfan.console --port 8765 --open
pause
