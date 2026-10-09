<#
.SYNOPSIS
    Removes Aero from this PC. Settings > Apps > Installed apps > Aero > Uninstall runs this script.

.DESCRIPTION
    The installer copies this file to the install folder (C:\Aero\Uninstall-Aero.ps1) and lists Aero under
    Installed apps with it as the uninstall command. It:
      1. asks for administrator rights when Aero was installed for every user (the normal install);
      2. shows what it will remove, with two boxes: delete downloaded models, delete chats/memory/settings
         (both ticked, so a plain "Uninstall" removes everything);
      3. stops Aero, its llama.cpp servers and its app window;
      4. deletes the Desktop and Start menu shortcuts that point into the install folder;
      5. deletes the install folder (or everything in it except the folders you chose to keep);
      6. deletes Aero's temporary files and its Installed apps entry.
    Anything Windows won't delete right away (a file still open) is deleted at the next sign-in.
    Python, and the Codex CLI or Claude Code if you installed them, are separate programs and are left alone.

.PARAMETER Quiet
    No windows. Keeps models and data unless -All, -RemoveModels or -RemoveData is given. Exit code 0 = removed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\Aero\Uninstall-Aero.ps1
    powershell -ExecutionPolicy Bypass -File C:\Aero\Uninstall-Aero.ps1 -Quiet -All
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$AppId = "Aero",
    [switch]$Quiet,
    [switch]$All,
    [switch]$RemoveModels,
    [switch]$RemoveData,
    [switch]$Elevated
)

$ErrorActionPreference = "Continue"
$UninstallKey = "Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppId"
$RunOnceKey = "Software\Microsoft\Windows\CurrentVersion\RunOnce"
$Log = Join-Path $env:TEMP "aero-uninstall.log"
if ($All) { $RemoveModels = $true; $RemoveData = $true }

function Log([string]$m) {
    try { Add-Content -LiteralPath $Log -Value ("{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $m) } catch {}
}

function Show-Message([string]$text, [string]$kind = "Information") {
    if ($Quiet) { return }
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show($text, "Uninstall Aero", "OK", $kind)
}

# ------------------------------------------------------------------------------------------- registry entry

function Open-Base([string]$hive) {
    $h = if ($hive -eq "HKLM") { [Microsoft.Win32.RegistryHive]::LocalMachine } else { [Microsoft.Win32.RegistryHive]::CurrentUser }
    # the 64-bit view, even when a 32-bit PowerShell runs this
    [Microsoft.Win32.RegistryKey]::OpenBaseKey($h, [Microsoft.Win32.RegistryView]::Registry64)
}

function Get-Entry {
    foreach ($hive in "HKLM", "HKCU") {
        $base = Open-Base $hive
        $k = $base.OpenSubKey($UninstallKey)
        if ($k) {
            $e = [pscustomobject]@{
                Hive     = $hive
                Location = [string]$k.GetValue("InstallLocation")
                Version  = [string]$k.GetValue("DisplayVersion")
                Name     = [string]$k.GetValue("DisplayName")
            }
            $k.Close(); $base.Close()
            return $e
        }
        $base.Close()
    }
    return $null
}

function Remove-Entry([string]$hive) {
    try {
        $base = Open-Base $hive
        $base.DeleteSubKeyTree($UninstallKey, $false)
        $base.Close()
        Log "Removed $hive\$UninstallKey"
        return $true
    } catch {
        Log "Could not remove $hive\$UninstallKey : $_"
        return $false
    }
}

# --------------------------------------------------------------------------------------------- folders

function Test-AeroRoot([string]$d) {
    if (-not $d -or -not (Test-Path -LiteralPath $d -PathType Container)) { return $false }
    if (Test-Path -LiteralPath (Join-Path $d "app\aero\__main__.py")) { return $true }
    $marker = Test-Path -LiteralPath (Join-Path $d "Uninstall-Aero.ps1")
    $parts = @("venv", "models", "data", "llama") | Where-Object { Test-Path -LiteralPath (Join-Path $d $_) }
    return ($marker -and @($parts).Count -gt 0)
}

function Test-SafeDir([string]$d) {
    # never delete a drive, Windows, Program Files, the profile folders or their direct parents
    $full = [IO.Path]::GetFullPath($d).TrimEnd("\")
    if ($full.Length -le 3) { return $false }
    $blocked = @($env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData, $env:USERPROFILE,
                 (Split-Path $env:USERPROFILE -Parent), $env:PUBLIC, $env:TEMP,
                 [Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("MyDocuments")) |
               Where-Object { $_ } | ForEach-Object { [IO.Path]::GetFullPath($_).TrimEnd("\") }
    return -not ($blocked -contains $full)
}

function Get-FolderBytes([string]$d) {
    if (-not (Test-Path -LiteralPath $d)) { return 0 }
    $sum = (Get-ChildItem -LiteralPath $d -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    if ($sum) { return [int64]$sum } else { return 0 }
}

function Format-Size([int64]$b) {
    if ($b -ge 1GB) { return "{0:N1} GB" -f ($b / 1GB) }
    if ($b -ge 1MB) { return "{0:N0} MB" -f ($b / 1MB) }
    if ($b -ge 1KB) { return "{0:N0} KB" -f ($b / 1KB) }
    return "$b bytes"
}

function Remove-Tree([string]$p) {
    if (-not (Test-Path -LiteralPath $p)) { return $true }
    for ($i = 0; $i -lt 5; $i++) {
        if (Test-Path -LiteralPath $p -PathType Container) {
            # rd with the \\?\ prefix handles paths longer than 260 characters (deep venv folders)
            & cmd.exe /d /c "rd /s /q `"\\?\$p`"" 2>$null | Out-Null
        } else {
            Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
        }
        if (-not (Test-Path -LiteralPath $p)) { return $true }
        if ($i -eq 1) { & attrib.exe -r -s -h "$p" /s /d 2>$null | Out-Null; & attrib.exe -r -s -h "$p\*" /s /d 2>$null | Out-Null }
        Start-Sleep -Milliseconds 800
    }
    return -not (Test-Path -LiteralPath $p)
}

# -------------------------------------------------------------------------------- where is Aero installed?

$entry = Get-Entry
if (-not $InstallDir) {
    $here = $PSScriptRoot
    if ($here -and (Split-Path $here -Leaf) -eq "installer" -and (Split-Path (Split-Path $here -Parent) -Leaf) -eq "app") {
        $here = Split-Path (Split-Path $here -Parent) -Parent        # run from <install>\app\installer
    }
    if (Test-AeroRoot $here) { $InstallDir = $here }
    elseif ($entry -and $entry.Location) { $InstallDir = $entry.Location }
    else { $InstallDir = "C:\Aero" }
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd("\")
Log "Start: dir=$InstallDir appid=$AppId quiet=$Quiet models=$RemoveModels data=$RemoveData elevated=$Elevated entry=$($entry.Hive)"

$registered = $entry -and $entry.Location -and ([IO.Path]::GetFullPath($entry.Location).TrimEnd("\") -ieq $InstallDir)
if (-not (Test-Path -LiteralPath $InstallDir)) {
    # the folder is already gone: only the Installed apps entry is left, so remove that
    if ($entry) {
        if ($entry.Hive -eq "HKLM" -and -not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -and -not $Elevated) {
            # falls through to the elevation step below
        } else {
            [void](Remove-Entry $entry.Hive)
            Show-Message "Aero's folder $InstallDir was already gone. Its entry in Installed apps has been removed."
            exit 0
        }
    } else {
        Show-Message "Aero isn't installed in $InstallDir." "Warning"
        exit 1
    }
} elseif (-not ((Test-AeroRoot $InstallDir) -or $registered) -or -not (Test-SafeDir $InstallDir)) {
    Log "Refused: $InstallDir doesn't look like an Aero install"
    Show-Message "$InstallDir doesn't look like an Aero install, so nothing was deleted." "Error"
    exit 1
}

# ------------------------------------------------------------------------------------- administrator rights

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$needAdmin = ($entry -and $entry.Hive -eq "HKLM")
if (-not $needAdmin -and (Test-Path -LiteralPath $InstallDir)) {
    try {
        $probe = Join-Path $InstallDir (".aero-write-test-" + [guid]::NewGuid().ToString("N"))
        [IO.File]::WriteAllText($probe, "x"); Remove-Item -LiteralPath $probe -Force
    } catch { $needAdmin = $true }
}
if ($needAdmin -and -not $isAdmin) {
    if ($Elevated) {
        Show-Message "Uninstalling Aero needs administrator rights, and Windows didn't grant them." "Error"
        exit 1
    }
    $argv = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", "`"$PSCommandPath`"",
              "-InstallDir", "`"$InstallDir`"", "-AppId", "`"$AppId`"", "-Elevated")
    if ($Quiet) { $argv += "-Quiet" }
    if ($RemoveModels) { $argv += "-RemoveModels" }
    if ($RemoveData) { $argv += "-RemoveData" }
    try {
        $p = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $argv -Verb RunAs -PassThru -Wait
        exit $p.ExitCode
    } catch {
        Log "Elevation declined: $_"
        Show-Message "Uninstalling Aero needs administrator rights. Nothing was changed." "Warning"
        exit 1602
    }
}

# --------------------------------------------------------------------------------------------- the dialog

$version = if ($entry -and $entry.Version) { $entry.Version } else { "" }
$modelsDir = Join-Path $InstallDir "models"
$dataDir = Join-Path $InstallDir "data"

if (-not $Quiet) {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()
    $modelsBytes = Get-FolderBytes $modelsDir
    $dataBytes = Get-FolderBytes $dataDir

    $form = New-Object System.Windows.Forms.Form
    $form.Text = "Uninstall Aero"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.StartPosition = "CenterScreen"
    $form.TopMost = $true
    $form.AutoSize = $true
    $form.AutoSizeMode = "GrowAndShrink"
    $form.Font = New-Object System.Drawing.Font("Segoe UI", 9)
    $form.Padding = New-Object System.Windows.Forms.Padding(8)
    $ico = Join-Path $InstallDir "aero-bubble.ico"
    if (Test-Path -LiteralPath $ico) { try { $form.Icon = New-Object System.Drawing.Icon($ico) } catch {} }

    $flow = New-Object System.Windows.Forms.FlowLayoutPanel
    $flow.FlowDirection = "TopDown"
    $flow.AutoSize = $true
    $flow.AutoSizeMode = "GrowAndShrink"
    $flow.WrapContents = $false
    $flow.Padding = New-Object System.Windows.Forms.Padding(8)
    $form.Controls.Add($flow)

    $title = New-Object System.Windows.Forms.Label
    $title.Text = ("Uninstall Aero " + $version).Trim()
    $title.Font = New-Object System.Drawing.Font("Segoe UI Semibold", 12)
    $title.AutoSize = $true
    $title.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 8)
    $flow.Controls.Add($title)

    $intro = New-Object System.Windows.Forms.Label
    $intro.Text = "This removes Aero from $InstallDir`: the app, its Python environment, llama.cpp, and its Start menu and desktop shortcuts."
    $intro.AutoSize = $true
    $intro.MaximumSize = New-Object System.Drawing.Size(460, 0)
    $intro.Margin = New-Object System.Windows.Forms.Padding(0, 0, 0, 10)
    $flow.Controls.Add($intro)

    $cbModels = New-Object System.Windows.Forms.CheckBox
    $cbModels.AutoSize = $true
    $cbModels.MaximumSize = New-Object System.Drawing.Size(460, 0)
    if ($modelsBytes -gt 0) {
        $cbModels.Text = "Delete downloaded models ($(Format-Size $modelsBytes))"
        $cbModels.Checked = $true
    } else {
        $cbModels.Text = "Delete the models folder (no models downloaded)"
        $cbModels.Checked = $true
    }
    $flow.Controls.Add($cbModels)

    $cbData = New-Object System.Windows.Forms.CheckBox
    $cbData.AutoSize = $true
    $cbData.MaximumSize = New-Object System.Drawing.Size(460, 0)
    $cbData.Text = "Delete chats, memory, settings and saved keys ($(Format-Size $dataBytes))"
    $cbData.Checked = $true
    $flow.Controls.Add($cbData)

    $note = New-Object System.Windows.Forms.Label
    $note.Text = "Models are in $modelsDir; chats, memory, settings, mods, tunings and saved keys are in $dataDir. Untick a box to keep that folder; installing Aero again picks it up."
    $note.AutoSize = $true
    $note.MaximumSize = New-Object System.Drawing.Size(460, 0)
    $note.ForeColor = [System.Drawing.SystemColors]::GrayText
    $note.Margin = New-Object System.Windows.Forms.Padding(0, 8, 0, 12)
    $flow.Controls.Add($note)

    $buttons = New-Object System.Windows.Forms.FlowLayoutPanel
    $buttons.FlowDirection = "RightToLeft"
    $buttons.AutoSize = $true
    $buttons.Width = 460
    $buttons.Anchor = "Right"
    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = "Cancel"
    $cancel.DialogResult = "Cancel"
    $cancel.AutoSize = $true
    $ok = New-Object System.Windows.Forms.Button
    $ok.Text = "Uninstall"
    $ok.DialogResult = "OK"
    $ok.AutoSize = $true
    $buttons.Controls.Add($cancel)
    $buttons.Controls.Add($ok)
    $flow.Controls.Add($buttons)
    $form.AcceptButton = $ok
    $form.CancelButton = $cancel

    if ($form.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        Log "Cancelled"
        exit 1602
    }
    $RemoveModels = $cbModels.Checked
    $RemoveData = $cbData.Checked
    $form.Dispose()

    $busy = New-Object System.Windows.Forms.Form
    $busy.Text = "Uninstall Aero"
    $busy.FormBorderStyle = "FixedDialog"
    $busy.ControlBox = $false
    $busy.StartPosition = "CenterScreen"
    $busy.TopMost = $true
    $busy.Size = New-Object System.Drawing.Size(380, 110)
    $busy.Font = New-Object System.Drawing.Font("Segoe UI", 9)
    $busyLabel = New-Object System.Windows.Forms.Label
    $busyLabel.Dock = "Fill"
    $busyLabel.TextAlign = "MiddleCenter"
    $busy.Controls.Add($busyLabel)
    $busy.Show()
}

function Step([string]$m) {
    Log $m
    if ($busy) { $busyLabel.Text = $m; $busy.Refresh(); [System.Windows.Forms.Application]::DoEvents() }
}

# ------------------------------------------------------------------------------------------- stop Aero

Step "Stopping Aero..."
$prefix = $InstallDir + "\"
$dataPrefix = (Join-Path $InstallDir "data") + "\"
function Get-AeroProcesses {
    # programs inside the install folder, browser windows using its data folder, and everything they started
    # (venv\Scripts\pythonw.exe is a launcher: the real Python it starts lives outside the folder)
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $ids = @{}
    foreach ($p in $all) {
        $exe = [string]$p.ExecutablePath
        $cmd = [string]$p.CommandLine
        $inside = $exe -and $exe.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
        $window = ($p.Name -in @("msedge.exe", "chrome.exe", "chromium.exe")) -and $cmd -and ($cmd.IndexOf($dataPrefix, [StringComparison]::OrdinalIgnoreCase) -ge 0)
        if (($inside -or $window) -and $p.ProcessId -ne $PID) { $ids[[int]$p.ProcessId] = $true }
    }
    do {
        $added = $false
        foreach ($p in $all) {
            if (-not $ids.ContainsKey([int]$p.ProcessId) -and $ids.ContainsKey([int]$p.ParentProcessId) -and $p.ProcessId -ne $PID) {
                $ids[[int]$p.ProcessId] = $true; $added = $true
            }
        }
    } while ($added)
    return @($all | Where-Object { $ids.ContainsKey([int]$_.ProcessId) })
}
$ours = Get-AeroProcesses
if ($ours | Where-Object { $_.CommandLine -like "*-m aero*" }) {
    # this install is the one running: let it shut down its model servers and MCP servers itself
    try { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8180/api/shutdown" -TimeoutSec 3 | Out-Null } catch {}
    Start-Sleep -Seconds 2
    $ours = Get-AeroProcesses
}
foreach ($p in $ours) {
    Log "Stopping $($p.Name) ($($p.ProcessId))"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 1

# --------------------------------------------------------------------------------------------- shortcuts

Step "Removing shortcuts..."
$shell = New-Object -ComObject WScript.Shell
$folders = @("Desktop", "CommonDesktopDirectory", "Programs", "CommonPrograms") |
           ForEach-Object { [Environment]::GetFolderPath($_) } | Where-Object { $_ } | Select-Object -Unique
foreach ($f in $folders) {
    foreach ($lnk in @(Get-ChildItem -LiteralPath $f -Filter *.lnk -File -ErrorAction SilentlyContinue)) {
        try { $target = [string]$shell.CreateShortcut($lnk.FullName).TargetPath } catch { $target = "" }
        if ($target -and $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $lnk.FullName -Force -ErrorAction SilentlyContinue
            Log "Removed shortcut $($lnk.FullName)"
        }
    }
}

# ------------------------------------------------------------------------------------------------- files

Step "Removing Aero's files..."
Set-Location -LiteralPath $env:TEMP          # nothing of ours keeps the folder open
$keep = @()
if (-not $RemoveModels -and (Test-Path -LiteralPath $modelsDir)) { $keep += "models" }
if (-not $RemoveData -and (Test-Path -LiteralPath $dataDir)) { $keep += "data" }
$left = @()
if ($keep.Count -eq 0) {
    if (-not (Remove-Tree $InstallDir)) { $left += $InstallDir }
} else {
    foreach ($item in @(Get-ChildItem -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue)) {
        if ($keep -contains $item.Name) { continue }
        if (-not (Remove-Tree $item.FullName)) { $left += $item.FullName }
    }
}
foreach ($t in @(Get-ChildItem -LiteralPath $env:TEMP -Force -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -like "aero-*" -and $_.Name -ne "aero-uninstall.log" })) {
    [void](Remove-Tree $t.FullName)
}

if ($left.Count) {
    # still open somewhere: delete at the next sign-in
    $hive = if ($isAdmin) { "HKLM" } else { "HKCU" }
    try {
        $base = Open-Base $hive
        $ro = $base.CreateSubKey($RunOnceKey)
        $n = 0
        foreach ($l in $left) {
            $n++
            $ro.SetValue("AeroCleanup$n", "cmd.exe /d /c rd /s /q `"$l`" 2>nul & del /f /q `"$l`" 2>nul")
        }
        $ro.Close(); $base.Close()
        Log "Scheduled for the next sign-in: $($left -join ', ')"
    } catch { Log "Could not schedule cleanup: $_" }
}

# ----------------------------------------------------------------------------------- Installed apps entry

Step "Removing Aero from Installed apps..."
if ($entry -and ($registered -or -not $entry.Location)) { [void](Remove-Entry $entry.Hive) }

if ($busy) { $busy.Close(); $busy.Dispose() }
$msg = "Aero was removed."
if ($keep.Count) { $msg += "`n`nKept:`n" + (($keep | ForEach-Object { Join-Path $InstallDir $_ }) -join "`n") }
if ($left.Count) { $msg += "`n`nWindows had these open, so they are deleted the next time you sign in:`n" + ($left -join "`n") }
Log ($msg -replace "`n", " | ")
Show-Message $msg
exit 0
