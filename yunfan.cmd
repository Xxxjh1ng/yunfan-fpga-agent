@echo off
rem YunFan launcher - runs the project inside its own .venv without activation.
rem Usage:  yunfan doctor
rem         yunfan design --spec examples\counter\spec.json
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] virtualenv not found: "%PY%"
    echo See the deployment guide under docs\ for setup steps.
    exit /b 1
)
"%PY%" -m yunfan %*
