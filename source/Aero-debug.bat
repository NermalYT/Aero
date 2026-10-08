@echo off
:: Runs Aero with a visible console so errors are shown. Use this if the Desktop icon does nothing.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
"%~dp0..\venv\Scripts\python.exe" -m aero
pause
