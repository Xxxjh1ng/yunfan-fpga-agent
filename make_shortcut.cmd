@echo off
rem Create a desktop shortcut for the YunFan console.
rem Double-click this file, then look for "YunFan Console" on your desktop.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1"
pause
