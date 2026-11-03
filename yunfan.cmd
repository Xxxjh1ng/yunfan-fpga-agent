@echo off
rem YunFan launcher - runs the project without activating a virtualenv.
rem Usage:  yunfan doctor
rem         yunfan console --open
rem         yunfan design --spec examples\counter\spec.json
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto run

set "PY=python"
python --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.8+ from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH", then try again.
    exit /b 1
)

:run
"%PY%" -m yunfan %*
