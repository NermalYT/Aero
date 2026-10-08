@echo off
:: Manual launcher (the Desktop shortcut does the same thing without a console window).
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"
start "" "%~dp0..\venv\Scripts\pythonw.exe" -m aero
