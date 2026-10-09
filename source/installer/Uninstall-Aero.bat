@echo off
:: Removes Aero. The same as Settings > Apps > Installed apps > Aero > Uninstall: it asks for administrator rights,
:: shows what it removes (models and chats can be kept) and deletes the shortcuts and the Installed apps entry.
:: Arguments go to Uninstall-Aero.ps1, e.g.  Uninstall-Aero.bat -Quiet -All
setlocal
set "PS1=%~dp0Uninstall-Aero.ps1"
if not exist "%PS1%" set "PS1=%~dp0app\installer\Uninstall-Aero.ps1"
if not exist "%PS1%" (
    echo Can't find Uninstall-Aero.ps1 next to this file. Uninstall Aero from Settings ^> Apps ^> Installed apps.
    pause
    exit /b 1
)
:: (goto) ends this script before the uninstaller deletes it, so cmd never reads the deleted file again
(goto) 2>nul & powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
