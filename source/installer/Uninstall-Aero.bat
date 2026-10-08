@echo off
setlocal EnableExtensions
title Aero Uninstaller

net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "DEST=C:\Aero"
echo.
echo  This removes Aero from %DEST% and deletes its shortcuts.
echo  Claude Code's own sign-in is not touched (sign out in Settings, Claude, before uninstalling if you want to).
echo.
choice /C YN /N /M "Also delete downloaded models in %DEST%\models (the router model too)? [Y/N] "
set "DELMODELS=%errorlevel%"
choice /C YN /N /M "Also delete chats, memory and settings in %DEST%\data? [Y/N] "
set "DELDATA=%errorlevel%"

powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8180/api/shutdown' -TimeoutSec 3 | Out-Null } catch {}" >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -like '*-m aero*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe' or Name='llama-bench.exe'\" | Where-Object { $_.ExecutablePath -like '%DEST%\*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
timeout /t 2 /nobreak >nul

for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do del /f /q "%%D\Aero.lnk" >nul 2>&1
del /f /q "%ProgramData%\Microsoft\Windows\Start Menu\Programs\Aero.lnk" >nul 2>&1

:: run the folder removal from %TEMP% so this script can delete its own folder
cd /d "%TEMP%"
for %%F in (app venv llama llama.new llama.old) do if exist "%DEST%\%%F" rmdir /s /q "%DEST%\%%F"
if "%DELMODELS%"=="1" if exist "%DEST%\models" rmdir /s /q "%DEST%\models"
if "%DELDATA%"=="1" if exist "%DEST%\data" rmdir /s /q "%DEST%\data"
for %%F in (README.md aero.ico aero-bubble.ico Update-Aero.bat) do del /f /q "%DEST%\%%F" >nul 2>&1
echo.
echo  Aero removed.
if exist "%DEST%\models" echo  Kept: %DEST%\models
if exist "%DEST%\data" echo  Kept: %DEST%\data
pause
(goto) 2>nul & del /f /q "%DEST%\Uninstall-Aero.bat" >nul 2>&1 & rmdir "%DEST%" >nul 2>&1
