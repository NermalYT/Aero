@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Aero Installer

:: ---------------------------------------------------------------------------
:: Aero installer (Update-Aero.bat runs this when Aero isn't installed yet)
::   - asks for administrator rights
::   - installs Python 3.12 if needed (winget, then python.org fallback)
::   - copies the app to C:\Aero and builds a private Python environment
::   - downloads the right llama.cpp build for your GPU (CUDA for NVIDIA)
::   - creates Desktop + Start Menu shortcuts that launch as administrator
::   - lets you pick the CPU decision router and a main model, then opens Aero
:: Update later with C:\Aero\Update-Aero.bat. Models, chats and settings are always kept.
:: ---------------------------------------------------------------------------

net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator rights...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "FROMOLD="
if /I "%~1"=="--from-old" set "FROMOLD=%~2"
for %%I in ("%~dp0..") do set "SRC=%%~fI"
set "DEST=C:\Aero"
set "PYVER=3.12.10"

echo.
echo  ==============================================================
echo    Aero installer
echo    Install folder: %DEST%
echo  ==============================================================
echo.

if not exist "%SRC%\aero\__main__.py" (
    echo [ERROR] Can't find Aero's source folder.
    echo         Extract the whole zip first, then run Update-Aero.bat from inside it.
    goto :fail
)

:: ---- 1. stop a running copy -----------------------------------------------
echo [1/7] Stopping any running Aero...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -like '*-m aero*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe' or Name='llama-bench.exe'\" | Where-Object { $_.ExecutablePath -like '%DEST%\*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe' or Name='chromium.exe'\" | Where-Object { $_.CommandLine -like '*%DEST%\data\*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

:: ---- 2. find or install Python ---------------------------------------------
echo [2/7] Looking for Python 3.10 - 3.13...
set "PY="
for %%V in (3.12 3.13 3.11 3.10) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    for %%P in ("%ProgramFiles%\Python312\python.exe" "%LocalAppData%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python313\python.exe" "%LocalAppData%\Programs\Python\Python313\python.exe") do (
        if not defined PY if exist %%P set "PY=%%~P"
    )
)
if not defined PY (
    echo       Python not found. Installing Python %PYVER%...
    winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements >nul 2>&1
    if exist "%ProgramFiles%\Python312\python.exe" set "PY=%ProgramFiles%\Python312\python.exe"
)
if not defined PY (
    echo       winget unavailable or failed; downloading from python.org...
    set "PYEXE=%TEMP%\python-%PYVER%-amd64.exe"
    powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe' -OutFile '!PYEXE!'"
    if not exist "!PYEXE!" (
        echo [ERROR] Could not download Python. Check your internet connection.
        goto :fail
    )
    "!PYEXE!" /quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_tcltk=1 Include_test=0
    if exist "%ProgramFiles%\Python312\python.exe" set "PY=%ProgramFiles%\Python312\python.exe"
)
if not defined PY (
    echo [ERROR] Python installation failed. Install Python 3.12 from python.org, then re-run this installer.
    goto :fail
)
if exist "%PY%" set "PY="%PY%""
echo       Using: %PY%

:: ---- 3. copy app files -------------------------------------------------------
echo [3/7] Copying Aero to %DEST%\app ...
if not exist "%DEST%" mkdir "%DEST%"
robocopy "%SRC%" "%DEST%\app" /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo [ERROR] Copy failed.
    goto :fail
)
copy /Y "%~dp0Uninstall-Aero.bat" "%DEST%\Uninstall-Aero.bat" >nul
if exist "%SRC%\..\Update-Aero.bat" copy /Y "%SRC%\..\Update-Aero.bat" "%DEST%\Update-Aero.bat" >nul
if exist "%SRC%\README.md" copy /Y "%SRC%\README.md" "%DEST%\README.md" >nul

:: ---- 4. private Python environment --------------------------------------------
echo [4/7] Creating the Python environment and installing packages (first run takes a few minutes)...
if not exist "%DEST%\venv\Scripts\python.exe" (
    %PY% -m venv "%DEST%\venv"
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        goto :fail
    )
)
set "VPY=%DEST%\venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q
"%VPY%" -m pip install --upgrade -r "%DEST%\app\requirements.txt" --disable-pip-version-check -q
if errorlevel 1 (
    echo [ERROR] Package installation failed. See the messages above.
    goto :fail
)

:: ---- 5. llama.cpp + icon + shortcuts ---------------------------------------------
echo [5/7] Installing llama.cpp for your GPU and creating shortcuts...
"%VPY%" "%DEST%\app\installer\setup.py" --dest "%DEST%"
if errorlevel 1 (
    echo [ERROR] llama.cpp setup failed. See the messages above.
    goto :fail
)

:: ---- 6. settings from the old install, then router + model chooser ---------------------
pushd "%DEST%\app"
if defined FROMOLD (
    echo [6/7] Carrying over your settings from %FROMOLD%, then models
    "%VPY%" -m aero.migrate --from "%FROMOLD%"
) else (
    echo [6/7] Models
    "%VPY%" -m aero.migrate --settings-only
)
"%VPY%" -m aero.setup_models
popd

:: ---- 7. done ---------------------------------------------------------------------
echo.
echo [7/7] Done.
echo.
echo  Aero is installed. Desktop icon: Aero (asks for admin on launch).
echo  Update later with %DEST%\Update-Aero.bat   Models: %DEST%\models   Chats/settings: %DEST%\data
if defined FROMOLD echo  Your models, chats, memory and settings moved here from %FROMOLD%.
where codex >nul 2>&1 || echo  Optional: to use your ChatGPT plan for reviews, install OpenAI's Codex CLI with
where codex >nul 2>&1 || echo            npm install -g @openai/codex   ^(then Settings ^> ChatGPT ^> Sign in^)
echo.
echo  Launching Aero...
start "" /D "%DEST%\app" "%DEST%\venv\Scripts\pythonw.exe" -m aero
timeout /t 6 /nobreak >nul
goto :end

:fail
echo.
echo  Installation did not finish.
pause
exit /b 1

:end
endlocal
exit /b 0
