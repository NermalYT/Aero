@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Aero Updater

:: ---------------------------------------------------------------------------
:: Aero updater
::   Run it from the extracted zip      -> installs or updates Aero, then the model chooser
::   Run it from C:\Aero             -> newest llama.cpp + packages, then the model chooser
::   Drop a newer Aero zip onto it   -> same as running it from the extracted zip
::   --auto                          -> what Aero's own updater passes: no questions, the open window reconnects
:: An older install under its earlier names (C:\Halcyon, or C:\VRAMpire before that) is moved to
:: C:\Aero the first time, with its models, chats, memory, settings and tunings.
:: Models, chats, settings and saved tunings are never deleted.
::
:: Options for a copy somewhere else (tests use them; without them nothing changes):
::   --dest FOLDER    install or update Aero in FOLDER instead of C:\Aero (or set AERO_INSTALL_DIR)
::   --skip-llama     leave llama.cpp as it is (no download)
::   --no-shortcuts   no Desktop or Start Menu shortcuts
::   --no-register    don't list Aero under Settings > Apps > Installed apps
::   --skip-models    don't run the model chooser
::   --lean           a new install leaves the integrations to Aero's background setup
::   --headless       start Aero without a window afterwards (Aero's updater passes it when Aero ran with --no-window)
:: AERO_INSTALL_FLAGS can hold the same options (Aero's own updater passes its environment on). AERO_PORT is the port
:: of the Aero to stop first; for a folder other than C:\Aero that port is only asked when AERO_PORT is set.
:: Administrator rights are asked for unless the install doesn't need them: a folder other than C:\Aero with
:: --no-shortcuts and --no-register. Only C:\Aero ever takes over an install under the app's earlier names.
:: ---------------------------------------------------------------------------

set "ARGFILE=%TEMP%\aero-update-arg.txt"
set "ARG1="
set "AUTO="
set "ELEVATED="
set "DEST="
set "NEXT="
set "SKIPLLAMA="
set "NOSHORTCUTS="
set "NOREGISTER="
set "SKIPMODELS="
set "LEAN="
set "HEADLESS="
for %%A in (%* %AERO_INSTALL_FLAGS%) do (
    set "OPT=%%~A"
    if "!NEXT!"=="dest" (
        set "DEST=%%~fA"
        set "NEXT="
    ) else if /I "%%~A"=="--auto" (
        set "AUTO=1"
    ) else if /I "%%~A"=="--elevated" (
        set "ELEVATED=1"
    ) else if /I "%%~A"=="--dest" (
        set "NEXT=dest"
    ) else if /I "%%~A"=="--skip-llama" (
        set "SKIPLLAMA=1"
    ) else if /I "%%~A"=="--no-shortcuts" (
        set "NOSHORTCUTS=1"
    ) else if /I "%%~A"=="--no-register" (
        set "NOREGISTER=1"
    ) else if /I "%%~A"=="--skip-models" (
        set "SKIPMODELS=1"
    ) else if /I "%%~A"=="--lean" (
        set "LEAN=1"
    ) else if /I "%%~A"=="--headless" (
        set "HEADLESS=1"
    ) else if "!OPT:~0,2!"=="--" (
        echo Ignoring unknown option %%~A
    ) else if not defined ARG1 (
        set "ARG1=%%~fA"
    )
)
set "FLAGS="
if defined SKIPLLAMA set "FLAGS=!FLAGS! --skip-llama"
if defined NOSHORTCUTS set "FLAGS=!FLAGS! --no-shortcuts"
if defined NOREGISTER set "FLAGS=!FLAGS! --no-register"
if defined SKIPMODELS set "FLAGS=!FLAGS! --skip-models"
if defined LEAN set "FLAGS=!FLAGS! --lean"
if defined HEADLESS set "FLAGS=!FLAGS! --headless"
set "SETUPFLAGS="
if defined SKIPLLAMA set "SETUPFLAGS=!SETUPFLAGS! --skip-llama"
if defined NOSHORTCUTS set "SETUPFLAGS=!SETUPFLAGS! --no-shortcuts"
if defined NOREGISTER set "SETUPFLAGS=!SETUPFLAGS! --no-register"

set "ZIP=!ARG1!"
if defined ELEVATED if exist "%ARGFILE%" (
    for /f "usebackq tokens=1* delims==" %%K in ("%ARGFILE%") do (
        if "%%K"=="ZIP" set "ZIP=%%L"
        if "%%K"=="DEST" set "DEST=%%L"
    )
    del /f /q "%ARGFILE%" >nul 2>&1
)
if not defined DEST if defined AERO_INSTALL_DIR for %%D in ("%AERO_INSTALL_DIR%") do set "DEST=%%~fD"
if not defined DEST set "DEST=C:\Aero"
if "!DEST:~-1!"=="\" set "DEST=!DEST:~0,-1!"
if "!DEST:~2!"=="" (
    echo [ERROR] Aero needs a folder of its own, not the root of a drive ^(!DEST!^).
    goto :fail
)
set "DEFAULTDEST="
if /I "!DEST!"=="C:\Aero" set "DEFAULTDEST=1"
set "NEEDADMIN=1"
if not defined DEFAULTDEST if defined NOSHORTCUTS if defined NOREGISTER set "NEEDADMIN="

net session >nul 2>&1
if errorlevel 1 if defined NEEDADMIN (
    powershell -NoProfile -Command "if ([Environment]::UserInteractive) { exit 0 } else { exit 1 }" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Updating !DEST! needs administrator rights, and nobody is here to approve Windows' prompt.
        echo         Run it as administrator, or for a test copy add --dest FOLDER --no-shortcuts --no-register.
        goto :fail
    )
    if exist "%ARGFILE%" del /f /q "%ARGFILE%"
    >"%ARGFILE%" (
        echo ZIP=!ZIP!
        echo DEST=!DEST!
    )
    set "PASS=--elevated!FLAGS!"
    if defined AUTO set "PASS=!PASS! --auto"
    echo Requesting administrator rights...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '!PASS!' -Verb RunAs"
    if errorlevel 1 (
        if exist "%ARGFILE%" del /f /q "%ARGFILE%"
        echo [ERROR] Windows didn't start the updater as administrator ^(the prompt was declined or couldn't be shown^).
        goto :fail
    )
    exit /b
)

set "PORT="
if defined AERO_PORT (
    set "PORT=%AERO_PORT%"
) else if defined DEFAULTDEST (
    set "PORT=8180"
)
set "OLD="
if defined DEFAULTDEST if not exist "%DEST%\venv\Scripts\python.exe" (
    if exist "C:\Halcyon\" (
        set "OLD=C:\Halcyon"
    ) else if exist "C:\VRAMpire\" (
        set "OLD=C:\VRAMpire"
    )
)
set "HERE=%~dp0"
set "ROOT="
set "MIGRATED="

echo.
echo  ==============================================================
echo    Aero updater
echo    Install folder: %DEST%
echo  ==============================================================
echo.

:: ---- where does new code come from? -------------------------------------------
if defined ZIP (
    if not exist "!ZIP!" (
        echo [ERROR] Can't find !ZIP!
        goto :fail
    )
    echo Unpacking !ZIP! ...
    set "XDIR=%TEMP%\aero-update"
    if exist "!XDIR!" rmdir /s /q "!XDIR!"
    powershell -NoProfile -Command "Expand-Archive -LiteralPath '!ZIP!' -DestinationPath '!XDIR!' -Force"
    if exist "!XDIR!\source\aero\__main__.py" set "ROOT=!XDIR!\"
    for /d %%D in ("!XDIR!\*") do if not defined ROOT if exist "%%D\source\aero\__main__.py" set "ROOT=%%D\"
    if not defined ROOT (
        echo [ERROR] That zip doesn't contain Aero ^(no source\aero folder^).
        goto :fail
    )
) else (
    if exist "%HERE%source\aero\__main__.py" set "ROOT=%HERE%"
)

:: ---- first run after the rename: move C:\Halcyon (or C:\VRAMpire) to C:\Aero ---------
if defined OLD (
    if not defined ROOT (
        echo [ERROR] Can't find the new Aero files next to this updater. Extract the whole zip first,
        echo         then run Update-Aero.bat from the extracted folder.
        goto :fail
    )
    echo Moving your install from !OLD! to %DEST% ^(models, chats, memory, settings and tunings come along^)...
    rem the old app's own shutdown also stops its MCP servers, which may have a folder inside it open
    powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8180/api/shutdown' -TimeoutSec 3 | Out-Null } catch {}" >nul 2>&1
    timeout /t 1 /nobreak >nul
    powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -like '*-m halcyon*' -or $_.CommandLine -like '*-m vrampire*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
    powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe' or Name='llama-bench.exe'\" | Where-Object { $_.ExecutablePath -like '!OLD!\*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
    rem the app window and the browser tool keep their Edge profiles in data\, which would block the move
    powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe' or Name='chromium.exe'\" | Where-Object { $_.CommandLine -like '*!OLD!\data\*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
    timeout /t 2 /nobreak >nul
    if exist "%DEST%\" rmdir "%DEST%" >nul 2>&1
    if exist "%DEST%\" (
        echo [ERROR] %DEST% already exists and isn't empty, so !OLD! can't be moved there.
        echo         Move or delete %DEST% yourself, then run this again.
        goto :fail
    )
    rem handles can take a moment to close after the processes above exit, so try a few times
    for /l %%N in (1,1,4) do if exist "!OLD!\" move "!OLD!" "%DEST%" >nul 2>&1 || timeout /t 2 /nobreak >nul
    if exist "!OLD!\" (
        echo [ERROR] Windows wouldn't move !OLD!. Close any Explorer window, terminal or editor that is
        echo         open inside it, then run this again. Nothing was changed.
        goto :fail
    )
    rem keep the old app's settings defaults (its "About you" text) until migrate has read them
    for %%P in (halcyon vrampire) do if exist "%DEST%\app\%%P\config.py" copy /Y "%DEST%\app\%%P\config.py" "%DEST%\data\legacy-config.py.txt" >nul
    rem A Python environment can't be moved safely, so the installer rebuilds it. That takes a few minutes.
    if exist "%DEST%\venv" rmdir /s /q "%DEST%\venv"
    for %%F in (Update-Halcyon.bat Uninstall-Halcyon.bat Update-VRAMpire.bat Uninstall-VRAMpire.bat README.md halcyon.ico halcyon-bubble.ico app\vrampire.ico) do if exist "%DEST%\%%F" del /f /q "%DEST%\%%F" >nul 2>&1
    set "MIGRATED=1"
    echo       Moved. Old shortcuts are replaced in a moment.
)

:: ---- not installed yet? hand over to the full installer ------------------------
if not exist "%DEST%\venv\Scripts\python.exe" (
    if defined ROOT if exist "!ROOT!source\installer\Install-Aero.bat" (
        if defined MIGRATED (
            call "!ROOT!source\installer\Install-Aero.bat" --from-old "!OLD!" --dest "!DEST!"!FLAGS!
        ) else (
            echo Aero isn't installed yet, running the full installer...
            call "!ROOT!source\installer\Install-Aero.bat" --dest "!DEST!"!FLAGS!
        )
        exit /b
    )
    echo [ERROR] Aero isn't installed in %DEST% and the new Aero files aren't next to this updater.
    echo         Extract the whole zip first, then run Update-Aero.bat from the extracted folder.
    goto :fail
)
set "VPY=%DEST%\venv\Scripts\python.exe"

:: ---- 1. stop Aero and its model servers -----------------------------------------
echo [1/6] Stopping Aero...
if defined PORT powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:!PORT!/api/shutdown' -TimeoutSec 3 | Out-Null } catch {}" >nul 2>&1
timeout /t 1 /nobreak >nul
rem only this install's Aero: its venv launcher, and the Python it starts (which the launcher takes down with it anyway)
powershell -NoProfile -Command "$all = @(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\"); $ours = @($all | Where-Object { $_.CommandLine -like '*-m aero*' -and $_.ExecutablePath -like '%DEST%\*' } | ForEach-Object { $_.ProcessId }); $all | Where-Object { $ours -contains $_.ProcessId -or $ours -contains $_.ParentProcessId } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe' or Name='llama-bench.exe'\" | Where-Object { $_.ExecutablePath -like '%DEST%\*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe' or Name='chromium.exe'\" | Where-Object { $_.CommandLine -like '*%DEST%\data\*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
timeout /t 2 /nobreak >nul

:: ---- 2. new app code (only when this runs from a newer zip) --------------------------
set "SELFUPDATE="
if defined ROOT (
    echo [2/6] Updating Aero from !ROOT!
    robocopy "!ROOT!source" "%DEST%\app" /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 (
        echo [ERROR] Copying the new code failed.
        goto :fail
    )
    if exist "%DEST%\app\installer\Uninstall-Aero.bat" copy /Y "%DEST%\app\installer\Uninstall-Aero.bat" "%DEST%\Uninstall-Aero.bat" >nul
    if exist "%DEST%\app\README.md" copy /Y "%DEST%\app\README.md" "%DEST%\README.md" >nul
    if /I "%~f0"=="%DEST%\Update-Aero.bat" (
        set "SELFUPDATE=!ROOT!Update-Aero.bat"
    ) else (
        copy /Y "%~f0" "%DEST%\Update-Aero.bat" >nul
    )
) else (
    echo [2/6] No newer Aero code next to this updater; keeping the installed app.
    echo       ^(To update the app itself, run the Update-Aero.bat inside a newer zip, or drop the zip onto this file.^)
)

:: ---- 3. Python packages ----------------------------------------------------------------
echo [3/6] Updating Python packages...
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q
"%VPY%" -m pip install --upgrade -r "%DEST%\app\requirements.txt" --disable-pip-version-check -q
if errorlevel 1 (
    echo [ERROR] Package update failed. See the messages above.
    goto :fail
)
if exist "%DEST%\app\requirements-extra.txt" (
    "%VPY%" -m pip install --upgrade -r "%DEST%\app\requirements-extra.txt" --disable-pip-version-check -q
    if errorlevel 1 echo       Warning: an optional package ^(browser tools or Claude plan reviews^) didn't install; Aero works without it.
)

:: ---- 4. llama.cpp, icon, shortcuts ----------------------------------------------------
if defined SKIPLLAMA (
    echo [4/6] llama.cpp: kept as it is ^(--skip-llama^).
) else (
    echo [4/6] Checking for a newer llama.cpp build for your GPU...
)
"%VPY%" "%DEST%\app\installer\setup.py" --dest "%DEST%"!SETUPFLAGS!
if errorlevel 1 (
    echo [ERROR] llama.cpp update failed. The previous build is still in place.
    goto :fail
)
pushd "%DEST%\app"
"%VPY%" -m aero.migrate --settings-only

:: ---- 5. router + model chooser ----------------------------------------------------------
if defined AUTO (
    echo [5/6] Models: kept as they are.
) else if defined SKIPMODELS (
    echo [5/6] Models: kept as they are ^(--skip-models^).
) else (
    echo [5/6] Models
    "%VPY%" -m aero.setup_models
)
popd

:: ---- 6. done --------------------------------------------------------------------------
echo.
echo [6/6] Updated. Launching Aero...
set "LAUNCH=-m aero"
if defined AUTO set "LAUNCH=!LAUNCH! --reopen"
if defined HEADLESS set "LAUNCH=!LAUNCH! --no-window"
start "" /D "%DEST%\app" "%DEST%\venv\Scripts\pythonw.exe" !LAUNCH!
timeout /t 5 /nobreak >nul
if not defined SELFUPDATE goto :end
:: replace this running script last: (goto) ends the script first, so cmd never reads the new file mid-run
(goto) 2>nul & copy /Y "%SELFUPDATE%" "%DEST%\Update-Aero.bat" >nul & exit /b 0

:end
endlocal
exit /b 0

:fail
echo.
echo  The update did not finish. Your models, chats and settings are untouched.
pause
exit /b 1
