<#
.SYNOPSIS
    Checks a real Aero install on this PC without changing it, and writes a report.

.DESCRIPTION
    Starts a second copy of Aero's backend from the installed code (C:\Aero\app) with a throwaway data folder under
    %USERPROFILE%\AeroTest\run-<time> and its own ports (8280 to 8285), so the installed app, its settings, chats,
    memory, models and shortcuts are never written to. It copies (reads) settings.json, models.json, tune_cache.json
    and hapo.json so an already-tuned model loads without tuning again. Model files are used where they are; nothing
    is copied, moved or deleted. API keys (secrets.json) are not copied, so no cloud model is ever called.

    Checks: hardware scan vs Windows' own view, the icon file, the shortcuts (read only), every listening socket is
    127.0.0.1, strict offline blocks a real outbound request and logs it, a model loads, a local chat streams (time to
    first token, tokens per second) with strict offline on, HAPO profiles, the benchmark suite, and optionally the
    router suite and a Windows Firewall test. Before and after, it fingerprints the real install's settings files to
    prove nothing changed.

    Output: REPORT.md and results.json in the run folder. Nothing is uploaded anywhere.

.PARAMETER AeroDir
    The Aero install folder. Default C:\Aero.

.PARAMETER Model
    Model id, or part of its name. Default: the model used most recently.

.PARAMETER Bench
    none, quick (default) or deep. Runs the Performance Lab benchmark suite against the loaded model.

.PARAMETER TuneLimitGB
    If the model was never tuned on this PC, tune a copy with this VRAM limit (GB) and the Short depth. The tuning is
    saved in the throwaway folder only. Without it, an untuned model is reported and skipped.

.PARAMETER Router
    Also start the decision router (if one is set up) and run the router suite.

.PARAMETER FirewallTest
    Needs an admin PowerShell. Adds temporary Windows Firewall rules that block outbound traffic for Aero's Python
    and llama-server.exe, checks that a real outbound request fails and a local chat still works, then removes the
    rules (also on errors or Ctrl+C). While the rules exist, other Python programs on this PC can't reach the internet.

.PARAMETER OpenUI
    At the end, open the throwaway copy in an app window for the checks that need a person, and wait for Enter.

.PARAMETER CompareTo
    An earlier run's results.json. The report then shows the change in every measured number.

.PARAMETER BasePort
    First of six ports to use. Default 8280.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\Aero\app\validation\Validate-Aero.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Validate-Aero.ps1 -Bench deep -Router -OpenUI

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Validate-Aero.ps1 -CompareTo "$env:USERPROFILE\AeroTest\run-20261008-2010\results.json"
#>
[CmdletBinding()]
param(
    [string]$AeroDir = "C:\Aero",
    [string]$Model = "",
    [ValidateSet("none", "quick", "deep")][string]$Bench = "quick",
    [double]$TuneLimitGB = 0,
    [switch]$Router,
    [switch]$FirewallTest,
    [switch]$OpenUI,
    [string]$CompareTo = "",
    [int]$BasePort = 8280
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 1
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
Add-Type -AssemblyName System.Net.Http

# ------------------------------------------------------------------------------------------------ results

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$RunDir = Join-Path $env:USERPROFILE "AeroTest\run-$Stamp"
$Home2 = Join-Path $RunDir "home"
New-Item -ItemType Directory -Force -Path (Join-Path $Home2 "data") | Out-Null

$R = [ordered]@{
    started  = (Get-Date).ToString("s")
    machine  = [ordered]@{}
    checks   = New-Object System.Collections.ArrayList
    metrics  = [ordered]@{}
    person   = New-Object System.Collections.ArrayList
    notes    = New-Object System.Collections.ArrayList
    run_dir  = $RunDir
}

function Add-Check([string]$Name, [string]$Status, [string]$Detail = "") {
    # Status: pass, fail, warn, skip
    [void]$R.checks.Add([ordered]@{ name = $Name; status = $Status; detail = $Detail })
    $color = @{ pass = "Green"; fail = "Red"; warn = "Yellow"; skip = "DarkGray" }[$Status]
    $tag = $Status.ToUpper().PadRight(4)
    Write-Host ("  [{0}] {1}" -f $tag, $Name) -ForegroundColor $color -NoNewline
    if ($Detail) { Write-Host "  $Detail" -ForegroundColor DarkGray } else { Write-Host "" }
}

function Add-Metric([string]$Name, $Value) { $R.metrics[$Name] = $Value }
function Add-Note([string]$Text) { [void]$R.notes.Add($Text); Write-Host "  note: $Text" -ForegroundColor DarkCyan }
function Section([string]$Title) { Write-Host ""; Write-Host "== $Title" -ForegroundColor Cyan }

# ------------------------------------------------------------------------------------------------ HTTP helpers

$Base = "http://127.0.0.1:$BasePort"
$Http = New-Object System.Net.Http.HttpClient
$Http.Timeout = [TimeSpan]::FromMinutes(90)

function Api([string]$Method, [string]$Path, $Body = $null, [int]$Timeout = 60) {
    $p = @{ Method = $Method; Uri = "$Base$Path"; TimeoutSec = $Timeout; UseBasicParsing = $true }
    if ($null -ne $Body) {
        $p.Body = [System.Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 20 -Compress))
        $p.ContentType = "application/json"
    }
    return Invoke-RestMethod @p
}

# POST a JSON body and read the server-sent events. Returns @{ events; first_ms; total_ms; stopped }. $FirstTypes are
# the event kinds that count as "first token" (chat content or reasoning). With $StopAfterSec, a chat that is still
# writing after that long is stopped through /api/stop (a looping model must not hang the run).
function Sse([string]$Path, $Body, [string[]]$FirstTypes = @(), [int]$TimeoutMin = 60, [int]$StopAfterSec = 0, $StopBody = $null) {
    $json = $Body | ConvertTo-Json -Depth 20 -Compress
    $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Post, "$Base$Path")
    $req.Content = New-Object System.Net.Http.StringContent($json, [System.Text.Encoding]::UTF8, "application/json")
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $resp = $Http.SendAsync($req, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).Result
    if (-not $resp.IsSuccessStatusCode) {
        $text = $resp.Content.ReadAsStringAsync().Result
        throw "HTTP $([int]$resp.StatusCode) from $Path : $text"
    }
    $reader = New-Object System.IO.StreamReader($resp.Content.ReadAsStreamAsync().Result, [System.Text.Encoding]::UTF8)
    $events = New-Object System.Collections.ArrayList
    $first = $null
    $stopped = $false
    $deadline = (Get-Date).AddMinutes($TimeoutMin)
    while (-not $reader.EndOfStream) {
        if ((Get-Date) -gt $deadline) { break }
        if ($StopAfterSec -gt 0 -and -not $stopped -and $sw.Elapsed.TotalSeconds -gt $StopAfterSec) {
            $stopped = $true
            try { [void](Api POST "/api/stop" $StopBody 10) } catch {}
        }
        $line = $reader.ReadLine()
        if (-not $line -or -not $line.StartsWith("data: ")) { continue }
        try { $ev = $line.Substring(6) | ConvertFrom-Json } catch { continue }
        [void]$events.Add($ev)
        $kind = $null
        if ($ev.PSObject.Properties["t"]) { $kind = $ev.t } elseif ($ev.PSObject.Properties["type"]) { $kind = $ev.type }
        if ($null -eq $first -and $FirstTypes -contains $kind) { $first = $sw.ElapsedMilliseconds }
        if ($kind -eq "log" -and $ev.PSObject.Properties["text"]) { Write-Host "      $($ev.text)" -ForegroundColor DarkGray }
    }
    $reader.Dispose(); $resp.Dispose()
    return @{ events = $events; first_ms = $first; total_ms = $sw.ElapsedMilliseconds; stopped = $stopped }
}

function Last-Result($Sse) {
    $res = $null; $err = $null
    foreach ($e in $Sse.events) {
        if ($e.PSObject.Properties["type"]) {
            if ($e.type -eq "result") { $res = $e.result }
            if ($e.type -eq "error") { $err = $e.error }
        }
    }
    return @{ result = $res; error = $err }
}

# One local chat turn with both review passes off. Returns the answer, the model's own stats and the client-side
# time to first token.
function Chat([string]$Text, [string]$Id, [int]$CapSec = 180) {
    $body = @{ chat_id = $Id; messages = @(@{ role = "user"; content = $Text }); opts = @{ review = "off"; chatgpt_review = "off"; think = "off" } }
    $r = Sse "/api/chat" $body @("content", "reasoning") 15 $CapSec @{ chat_id = $Id }
    $answer = ""; $stats = $null; $err = $null
    foreach ($e in $r.events) {
        if (-not $e.PSObject.Properties["t"]) { continue }
        if ($e.t -eq "content" -and (-not $e.PSObject.Properties["lane"] -or $e.lane -eq "local")) { $answer += $e.d }
        if ($e.t -eq "assistant_done" -and $e.message.PSObject.Properties["stats"]) { $stats = $e.message.stats }
        if ($e.t -eq "error") { $err = $e.error }
    }
    return @{ answer = $answer; stats = $stats; error = $err; first_ms = $r.first_ms; total_ms = $r.total_ms; stopped = $r.stopped }
}

function Port-Free([int]$Port) {
    try { return -not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop) } catch { return $true }
}

# ------------------------------------------------------------------------------------------------ fingerprint

$WatchFiles = @("settings.json", "models.json", "router.json", "tune_cache.json", "hapo.json", "memory.json",
                "mcp.json", "secrets.json")

function Fingerprint {
    $fp = [ordered]@{}
    foreach ($f in $WatchFiles) {
        $p = Join-Path $AeroDir "data\$f"
        if (Test-Path -LiteralPath $p) { $fp[$f] = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash } else { $fp[$f] = "(none)" }
    }
    $chats = Join-Path $AeroDir "data\chats"
    $fp["chats"] = if (Test-Path $chats) { @(Get-ChildItem $chats -Filter *.json).Count } else { 0 }
    $models = Join-Path $AeroDir "models"
    $fp["model_files"] = if (Test-Path $models) {
        (@(Get-ChildItem $models -Recurse -Filter *.gguf -ErrorAction SilentlyContinue | ForEach-Object { "$($_.FullName)|$($_.Length)" }) -join ";").GetHashCode()
    } else { 0 }
    return $fp
}

# ------------------------------------------------------------------------------------------------ preflight

Write-Host ""
Write-Host "Aero validation  ->  $RunDir" -ForegroundColor White
Section "Preflight"

$Py = Join-Path $AeroDir "venv\Scripts\python.exe"
$App = Join-Path $AeroDir "app"
$Llama = Join-Path $AeroDir "llama"
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$os = Get-CimInstance Win32_OperatingSystem
$R.machine.os = "$($os.Caption) $($os.Version)"
$R.machine.powershell = $PSVersionTable.PSVersion.ToString()
$R.machine.admin = $IsAdmin

if (-not (Test-Path -LiteralPath $Py)) { Add-Check "Aero's Python environment" "fail" "$Py not found. Install Aero first."; throw "Aero is not installed in $AeroDir" }
Add-Check "Aero's Python environment" "pass" $Py
if (-not (Test-Path -LiteralPath (Join-Path $App "aero\__main__.py"))) { Add-Check "Aero app code" "fail" "$App\aero missing"; throw "Aero app code missing" }
$ver = & $Py -c "import sys; sys.path.insert(0, r'$App'); from aero.config import VERSION; print(VERSION)" 2>$null
Add-Check "Aero app code" "pass" "version $ver"
$R.machine.aero = "$ver"

$server = Get-ChildItem -LiteralPath $Llama -Recurse -Filter llama-server.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($server) {
    $lv = ""; $vf = Join-Path $Llama "VERSION.txt"
    if (Test-Path $vf) { $lv = (Get-Content $vf -Raw).Trim() }
    Add-Check "llama.cpp installed" "pass" "$lv  ($($server.FullName))"
    $R.machine.llama = $lv
} else {
    Add-Check "llama.cpp installed" "fail" "no llama-server.exe under $Llama. Run Update-Aero.bat."
}

try {
    $live = Invoke-RestMethod -Uri "http://127.0.0.1:8180/api/state" -TimeoutSec 2 -UseBasicParsing
    Add-Note "Your installed Aero is running (status $($live.engine.status)). That's fine; this copy uses other ports, but both share the GPU, so speeds may be lower than usual."
} catch {}

$busy = @(); foreach ($p in $BasePort..($BasePort + 5)) { if (-not (Port-Free $p)) { $busy += $p } }
if ($busy.Count) { Add-Check "Ports $BasePort-$($BasePort + 5) free" "fail" "in use: $($busy -join ', '). Use -BasePort 8380."; throw "ports busy" }
Add-Check "Ports $BasePort-$($BasePort + 5) free" "pass"

$Before = Fingerprint

# ------------------------------------------------------------------------------------------------ hardware (Windows' own view)

Section "Hardware (as Windows reports it)"
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$R.machine.cpu = "$($cpu.Name.Trim()) ($($cpu.NumberOfCores) cores, $($cpu.NumberOfLogicalProcessors) threads)"
$R.machine.ram_gb = $ramGB
Write-Host "  CPU  $($R.machine.cpu)"
Write-Host "  RAM  $ramGB GB"

$winGpus = @()
$cls = "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
foreach ($k in Get-ChildItem $cls -ErrorAction SilentlyContinue) {
    if ($k.PSChildName -notmatch '^\d+$') { continue }
    $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
    if (-not $p -or -not $p.PSObject.Properties["DriverDesc"]) { continue }
    $mem = 0
    if ($p.PSObject.Properties["HardwareInformation.qwMemorySize"]) {
        $raw = $p."HardwareInformation.qwMemorySize"
        if ($raw -is [byte[]]) { $mem = [BitConverter]::ToUInt64($raw, 0) } else { $mem = [uint64]$raw }
    }
    $winGpus += [ordered]@{ name = $p.DriverDesc; vram_mb = [math]::Round($mem / 1MB); driver = $p.DriverVersion }
    Write-Host ("  GPU  {0}  {1:N0} MB  driver {2}" -f $p.DriverDesc, ($mem / 1MB), $p.DriverVersion)
}
$R.machine.gpus_windows = $winGpus
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $q = & nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version,temperature.gpu,power.draw --format=csv,noheader,nounits 2>$null
    $R.machine.nvidia_smi = @($q)
    foreach ($l in @($q)) { Write-Host "  nvidia-smi  $l" }
}

# ------------------------------------------------------------------------------------------------ icon and shortcuts (read only)

Section "Icon and shortcuts (read only)"
function Ico-Sizes([string]$Path) {
    $b = [System.IO.File]::ReadAllBytes($Path)
    if ($b.Length -lt 6 -or [BitConverter]::ToUInt16($b, 2) -ne 1) { return @() }
    $n = [BitConverter]::ToUInt16($b, 4); $sizes = @()
    for ($i = 0; $i -lt $n; $i++) { $w = $b[6 + 16 * $i]; if ($w -eq 0) { $w = 256 }; $sizes += [int]$w }
    return $sizes
}
$ico = Join-Path $AeroDir "aero-bubble.ico"
if (Test-Path -LiteralPath $ico) {
    $sizes = Ico-Sizes $ico
    $need = 16, 20, 24, 32, 40, 48, 64, 96, 128, 256
    $missing = @($need | Where-Object { $sizes -notcontains $_ })
    if ($missing.Count) { Add-Check "App icon sizes" "warn" "has $($sizes -join ', '); missing $($missing -join ', ')" }
    else { Add-Check "App icon sizes" "pass" "$($sizes -join ', ') px" }
} else { Add-Check "App icon file" "fail" "$ico not found" }

$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath("Desktop")
$startMenu = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"
foreach ($folder in @($desktop, $startMenu)) {
    $lnk = Join-Path $folder "Aero.lnk"
    if (-not (Test-Path -LiteralPath $lnk)) { Add-Check "Shortcut $lnk" "warn" "not found"; continue }
    $s = $shell.CreateShortcut($lnk)      # reading only; Save() is never called
    $bytes = [System.IO.File]::ReadAllBytes($lnk)
    $runas = ($bytes[0x15] -band 0x20) -ne 0
    $ok = ($s.TargetPath -like "*\venv\Scripts\pythonw.exe") -and ($s.Arguments -eq "-m aero") -and ($s.IconLocation -like "*aero-bubble.ico*")
    $detail = "target $($s.TargetPath) $($s.Arguments); icon $($s.IconLocation); run as admin: $runas"
    if ($ok) { Add-Check "Shortcut $lnk" "pass" $detail } else { Add-Check "Shortcut $lnk" "warn" $detail }
    foreach ($old in @("Halcyon.lnk", "VRAMpire.lnk")) {
        if (Test-Path -LiteralPath (Join-Path $folder $old)) { Add-Check "Old shortcut $old removed" "warn" "still in $folder" }
    }
}
[void]$R.person.Add("Look at the Desktop and taskbar icon at your display scaling: it should be the iridescent bubble, crisp, never a frog.")

# ------------------------------------------------------------------------------------------------ throwaway copy

Section "Starting a throwaway copy of Aero"
foreach ($f in @("settings.json", "models.json", "tune_cache.json", "hapo.json")) {
    $src = Join-Path $AeroDir "data\$f"
    if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination (Join-Path $Home2 "data\$f") }
}
if ($Router) {
    $src = Join-Path $AeroDir "data\router.json"
    if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination (Join-Path $Home2 "data\router.json") }
}
Write-Host "  Copied your settings and tunings (read only) into $Home2\data"

$saved = @{}
$envs = @{
    AERO_HOME         = $Home2
    AERO_PORT         = "$BasePort"
    AERO_LLAMA_PORT   = "$($BasePort + 1)"
    AERO_OVERLAY_PORT = "$($BasePort + 5)"
    AERO_LLAMA_DIR    = $Llama
}
foreach ($k in $envs.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, $envs[$k]) }
$code = "import uvicorn; from aero.server import app; uvicorn.run(app, host='127.0.0.1', port=$BasePort, log_level='warning')"
$Proc = Start-Process -FilePath $Py -ArgumentList @("-c", "`"$code`"") -WorkingDirectory $App -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $RunDir "backend.out.txt") -RedirectStandardError (Join-Path $RunDir "backend.err.txt")
foreach ($k in $envs.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }

$FwGroup = "Aero validation (temporary)"
$State = $null
try {
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        try { $State = Api GET "/api/state" $null 5; break } catch { Start-Sleep -Milliseconds 700 }
        if ($Proc.HasExited) { break }
    }
    if (-not $State) {
        $err = Get-Content (Join-Path $RunDir "backend.err.txt") -Tail 20 -ErrorAction SilentlyContinue
        Add-Check "Backend starts" "fail" ($err -join " | ")
        throw "The backend did not start"
    }
    Add-Check "Backend starts" "pass" "$($State.app) $($State.version) on $Base (pid $($Proc.Id))"
    if ($State.llama.found) { Add-Check "Backend finds llama-server" "pass" $State.llama.path } else { Add-Check "Backend finds llama-server" "fail" }

    # ---------------------------------------------------------------------------------------- Aero's hardware scan
    Section "Aero's hardware scan"
    $hw = Api GET "/api/hw"
    $R.machine.aero_hw = $hw
    Write-Host "  CPU  $($hw.cpu) ($($hw.cores) cores / $($hw.threads) threads), RAM $([math]::Round($hw.ram_total_mb / 1024)) GB"
    foreach ($g in @($hw.gpus)) { Write-Host ("  GPU  {0}  {1:N0} MB  ({2}, {3})" -f $g.name, $g.total_mb, $g.vendor, $(if ($g.measured) { "live VRAM readings" } else { "estimated use" })) }
    $dedicated = @($winGpus | Where-Object { $_.vram_mb -ge 2048 })
    if (@($hw.gpus).Count -ge 1 -or $dedicated.Count -eq 0) {
        Add-Check "GPUs found" "pass" "$(@($hw.gpus).Count) usable GPU(s); Windows lists $($winGpus.Count) display adapter(s)"
    } else {
        Add-Check "GPUs found" "fail" "Windows lists a GPU with $($dedicated[0].vram_mb) MB, Aero found none"
    }
    foreach ($g in @($hw.gpus)) {
        $w = $winGpus | Where-Object { $_.name -eq $g.name } | Select-Object -First 1
        if ($w -and $w.vram_mb -gt 0) {
            $diff = [math]::Abs($w.vram_mb - $g.total_mb)
            if ($diff -le 512) { Add-Check "VRAM size of $($g.name)" "pass" "$($g.total_mb) MB" }
            else { Add-Check "VRAM size of $($g.name)" "warn" "Aero $($g.total_mb) MB, registry $($w.vram_mb) MB" }
        }
    }
    $cat = Api GET "/api/catalog"
    $rec = @($cat.main | Where-Object { $_.recommended }) | Select-Object -First 1
    $rrec = @($cat.routers | Where-Object { $_.recommended }) | Select-Object -First 1
    if ($rec) {
        $fit = $rec.plan
        Add-Check "Recommended model for this PC" "pass" ("{0} {1} (runs: {2}, {3} GB)" -f $rec.name, $fit.quant, $fit.fit, $fit.size_gb)
        Add-Metric "recommended_model" "$($rec.name) $($fit.quant)"
    }
    if ($rrec) { Add-Check "Recommended router for this PC" "pass" $rrec.name; Add-Metric "recommended_router" $rrec.name }

    # ---------------------------------------------------------------------------------------- cloud connections (status only)
    Section "Cloud connections (status only, nothing is sent)"
    try {
        $gpt = Api GET "/api/chatgpt"
        $plan = Api GET "/api/chatgpt/plan" $null 60
        $how = if ($plan.loggedIn) { "signed in ($($plan.method))" } elseif ($plan.installed) { "Codex CLI installed, not signed in" } else { "Codex CLI not installed" }
        Add-Check "ChatGPT connection" "pass" "backend: $($gpt.backend); plan: $how; API key in this copy: none (not copied)"
    } catch { Add-Check "ChatGPT connection" "warn" "$_" }
    try {
        $cl = Api GET "/api/cloud"
        $cp = Api GET "/api/claude/plan" $null 60
        $how = if ($cp.PSObject.Properties["loggedIn"] -and $cp.loggedIn) { "signed in" } else { "not signed in" }
        Add-Check "Claude connection" "pass" "backend: $($cl.backend); plan: $how"
    } catch { Add-Check "Claude connection" "warn" "$_" }
    [void]$R.person.Add("Optional: with ChatGPT and/or Claude connected, send one request with both review buttons on and check that ChatGPT's pass (green, then orange) comes first and Claude's (violet, then amber) second.")

    # ---------------------------------------------------------------------------------------- strict offline
    Section "Strict offline"
    [void](Api PUT "/api/settings" @{ strict_offline = $true })
    $blocked = $false
    try { [void](Api GET "/api/hf/search?q=qwen" $null 20) } catch { $blocked = $true }
    $audit = Join-Path $Home2 "data\audit\network.jsonl"
    $line = $null
    if (Test-Path $audit) { $line = Get-Content $audit | Where-Object { $_ -match "huggingface" -and $_ -match '"allowed": false' } | Select-Object -Last 1 }
    if ($blocked -and $line) { Add-Check "Strict offline blocks a real outbound request" "pass" "Hugging Face search refused and logged" }
    elseif ($blocked) { Add-Check "Strict offline blocks a real outbound request" "warn" "refused, but no audit line found in $audit" }
    else { Add-Check "Strict offline blocks a real outbound request" "fail" "the search went through" }

    # ---------------------------------------------------------------------------------------- load a model
    Section "Model"
    $ml = Api GET "/api/models"
    $list = @()
    $list = @($ml | Where-Object { $_.PSObject.Properties["exists"] -and $_.exists })
    $pick = $null
    if ($Model) { $pick = $list | Where-Object { $_.id -eq $Model -or $_.name -like "*$Model*" } | Select-Object -First 1 }
    if (-not $pick) { $pick = $list | Sort-Object { if ($_.PSObject.Properties["last_used"] -and $_.last_used) { [double]$_.last_used } else { 0 } } -Descending | Select-Object -First 1 }
    $Loaded = $false
    if (-not $pick) {
        Add-Check "Load a model" "skip" "no models in $AeroDir\data\models.json"
    } else {
        Write-Host "  Loading $($pick.name) ..."
        $res = Last-Result (Sse "/api/load" @{ id = $pick.id } @() 30)
        if ($res.result -and $res.result.PSObject.Properties["needs_tune"] -and $res.result.needs_tune) {
            if ($TuneLimitGB -gt 0) {
                Write-Host "  Not tuned on this PC yet: tuning a copy (Short, $TuneLimitGB GB) ..."
                $sw = [Diagnostics.Stopwatch]::StartNew()
                $res = Last-Result (Sse "/api/load" @{ id = $pick.id; vram_limit_gb = $TuneLimitGB; depth = "short" } @() 120)
                Add-Metric "tune_seconds" ([math]::Round($sw.Elapsed.TotalSeconds))
            } else {
                Add-Check "Load a model" "skip" "$($pick.name) isn't tuned on this PC. Rerun with -TuneLimitGB <GB> to tune a throwaway copy."
                $res = $null
            }
        }
        if ($res -and $res.result -and $res.result.PSObject.Properties["ready"]) {
            $Loaded = $true
            Add-Check "Load a model" "pass" "$($pick.name): $($res.result.desc)"
            Add-Metric "model" $pick.name
            Add-Metric "model_config" $res.result.desc
            Add-Metric "tuned_decode_tps" $res.result.tg
            Add-Metric "tuned_prefill_tps" $res.result.pp
            if ($res.result.warning) { Add-Note $res.result.warning }
        } elseif ($res -and $res.error) {
            Add-Check "Load a model" "fail" $res.error
        }
    }

    # ---------------------------------------------------------------------------------------- sockets
    $ls = Api GET "/api/local_status"
    if ($null -eq $ls.all_loopback) { Add-Check "Every listening socket is 127.0.0.1" "warn" $ls.listeners_note }
    elseif ($ls.all_loopback) { Add-Check "Every listening socket is 127.0.0.1" "pass" (@($ls.listeners | ForEach-Object { "$($_.process) :$($_.port)" }) -join ", ") }
    else { Add-Check "Every listening socket is 127.0.0.1" "fail" (@($ls.listeners | Where-Object { -not $_.loopback } | ForEach-Object { "$($_.process) $($_.address):$($_.port)" }) -join ", ") }

    if ($Loaded) {
        $st = Api GET "/api/stats"
        if ($st.engine.vram_mb) { Add-Metric "model_vram_mb" $st.engine.vram_mb }

        # ------------------------------------------------------------------------------------ local chat
        Section "Local chat (strict offline on)"
        $c1 = Chat "Reply with exactly these two words: Aero OK" "validate-1" 120
        if ($c1.error) { Add-Check "Local chat streams" "fail" $c1.error }
        elseif ($c1.stopped) { Add-Check "Local chat streams" "warn" ("streamed (first token {0} ms) but was still writing after 120 s, so it was stopped; the model may be looping" -f $c1.first_ms); Add-Metric "chat_ttft_ms" $c1.first_ms }
        else {
            $ok = $c1.answer -match "Aero OK"
            $tps = if ($c1.stats -and $c1.stats.PSObject.Properties["tg"]) { $c1.stats.tg } else { $null }
            Add-Check "Local chat streams" $(if ($ok) { "pass" } else { "warn" }) ("first token {0} ms, total {1} ms, {2} tok/s; answer: {3}" -f $c1.first_ms, $c1.total_ms, $tps, ($c1.answer.Trim() -replace "\s+", " ").Substring(0, [math]::Min(60, $c1.answer.Trim().Length)))
            Add-Metric "chat_ttft_ms" $c1.first_ms
            Add-Metric "chat_total_ms" $c1.total_ms
            if ($tps) { Add-Metric "chat_decode_tps" $tps }
        }
        $c2 = Chat ("Write a 200 word paragraph about soap bubbles.") "validate-2"
        if (-not $c2.error -and -not $c2.stopped -and $c2.stats) {
            Add-Metric "long_reply_ttft_ms" $c2.first_ms
            if ($c2.stats.PSObject.Properties["tg"]) { Add-Metric "long_reply_decode_tps" $c2.stats.tg }
            Add-Check "Longer reply" "pass" ("first token {0} ms, {1} tok/s" -f $c2.first_ms, $c2.stats.tg)
        }

        # ------------------------------------------------------------------------------------ HAPO
        Section "HAPO profiles"
        try {
            $hp = Api GET "/api/hapo/$($pick.id)"
            if ($hp.tuned) {
                $names = @()
                if ($hp.PSObject.Properties["profiles"]) { $names = @($hp.profiles | ForEach-Object { if ($_.PSObject.Properties["name"]) { $_.name } else { $_.profile } }) }
                Add-Check "HAPO profiles from the measured trials" "pass" ($names -join ", ")
                if ($hp.PSObject.Properties["stale"] -and $hp.stale) { Add-Note "The active profile was made on different hardware or drivers; Aero suggests re-tuning." }
            } else { Add-Check "HAPO profiles" "skip" "model not tuned" }
        } catch { Add-Check "HAPO profiles" "warn" "$_" }

        # ------------------------------------------------------------------------------------ benchmark
        if ($Bench -ne "none") {
            Section "Benchmark ($Bench)"
            [void](Api PUT "/api/settings" @{ strict_offline = $false })
            $b = Last-Result (Sse "/api/bench/run" @{ suite = $Bench; scenery = "none (validation, no window open)"; save = $true } @() 120)
            if ($b.error) { Add-Check "Benchmark suite" "fail" $b.error }
            elseif ($b.result) {
                Add-Check "Benchmark suite" "pass" "saved in $Home2\data\bench"
                $R.metrics["bench"] = $b.result.results
                try { $md = Invoke-WebRequest -Uri "$Base/api/lab/report?mid=$([uri]::EscapeDataString($pick.id))&fmt=md" -UseBasicParsing -TimeoutSec 30; Set-Content -Path (Join-Path $RunDir "bench-report.md") -Value $md.Content -Encoding UTF8 } catch {}
            }
            [void](Api PUT "/api/settings" @{ strict_offline = $true })
        }
    }

    # ---------------------------------------------------------------------------------------- router
    if ($Router) {
        Section "Router"
        $rt = Api GET "/api/router"
        if (-not $rt.status.configured) {
            Add-Check "Router suite" "skip" "no router set up (Settings > Router, or run Update-Aero.bat)"
        } else {
            $deadline = (Get-Date).AddSeconds(120)
            while ((Get-Date) -lt $deadline -and -not (Api GET "/api/router").status.ready) { Start-Sleep 2 }
            $rs = (Api GET "/api/router").status
            if (-not $rs.ready) { Add-Check "Router starts" "fail" ("$($rs.error)") }
            else {
                Add-Check "Router starts" "pass" "$($rs.model), $($rs.threads) threads"
                $b = Last-Result (Sse "/api/bench/router" @{} @() 60)
                if ($b.error) { Add-Check "Router suite" "fail" $b.error } else { Add-Check "Router suite" "pass"; $R.metrics["router"] = $b.result.results.router }
            }
        }
    }

    # ---------------------------------------------------------------------------------------- firewall
    if ($FirewallTest) {
        Section "Firewall test"
        if (-not $IsAdmin) { Add-Check "Firewall test" "skip" "run PowerShell as administrator for this test" }
        elseif (-not $Loaded) { Add-Check "Firewall test" "skip" "needs a loaded model" }
        else {
            $basePy = (& $Py -c "import sys; print(sys._base_executable)").Trim()
            $progs = @($Py, $basePy, $server.FullName) | Select-Object -Unique
            foreach ($p in $progs) {
                New-NetFirewallRule -DisplayName "Aero validation: block $([IO.Path]::GetFileName($p))" -Group $FwGroup `
                    -Direction Outbound -Action Block -Program $p -Profile Any | Out-Null
            }
            Write-Host "  Temporary outbound block rules added for: $($progs -join ', ')"
            [void](Api PUT "/api/settings" @{ strict_offline = $false })
            $reached = $true
            try { [void](Api GET "/api/hf/search?q=qwen" $null 30) } catch { $reached = $false }
            if (-not $reached) { Add-Check "Firewall blocks Aero's outbound traffic" "pass" "Hugging Face search failed with strict offline off" }
            else { Add-Check "Firewall blocks Aero's outbound traffic" "fail" "a request still went out" }
            $c3 = Chat "Reply with exactly these two words: Aero OK" "validate-fw" 120
            if ($c3.error) { Add-Check "Local chat with outbound traffic blocked" "fail" $c3.error }
            else { Add-Check "Local chat with outbound traffic blocked" "pass" ("first token {0} ms" -f $c3.first_ms) }
            Remove-NetFirewallRule -Group $FwGroup -ErrorAction SilentlyContinue
            Write-Host "  Temporary rules removed."
        }
    } else {
        [void]$R.person.Add("Optional, as administrator: rerun with -FirewallTest to prove a local chat works with Aero's outbound traffic blocked by Windows Firewall.")
    }

    # ---------------------------------------------------------------------------------------- the window
    [void]$R.person.Add("Scenery: Settings > Appearance > Full, Still and Off. Full animates (bubbles, clouds, ribbons, frogs breathing); Still keeps the same picture without motion; Off is a plain sky.")
    [void]$R.person.Add("Frogs: three poses in the pond, mouths closed; every so often one meeps (mouth open about a second). Clicking a frog makes it meep. No frog ever covers a button or text, including at narrow window widths.")
    [void]$R.person.Add("Windows Settings > Accessibility > Visual effects > Animation effects off: the scenery should stop moving.")
    [void]$R.person.Add("The scenery pauses while a reply is being written and when the window is minimized.")
    [void]$R.person.Add("Day and night themes, at 100%, 125%, 150% and 200% display scaling: text stays readable on the glass.")
    [void]$R.person.Add("Composer: the ChatGPT and Claude buttons each cycle off / on / auto and are clearly different colours.")
    if ($OpenUI) {
        Section "Window"
        $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
        $prof = Join-Path $Home2 "data\ui-profile"
        if ($edge) { Start-Process $edge -ArgumentList @("--app=$Base", "--user-data-dir=`"$prof`"", "--window-size=1280,860", "--no-first-run") }
        else { Start-Process $Base }
        Write-Host "  The throwaway copy is open. Go through the list below, then press Enter here to finish." -ForegroundColor Yellow
        foreach ($p in $R.person) { Write-Host "   - $p" }
        [void](Read-Host)
    }
}
finally {
    Section "Cleaning up"
    try { Remove-NetFirewallRule -Group $FwGroup -ErrorAction SilentlyContinue } catch {}
    try { [void](Invoke-RestMethod -Method Post -Uri "$Base/api/shutdown" -TimeoutSec 5 -UseBasicParsing) } catch {}
    Start-Sleep -Seconds 2
    if ($Proc -and -not $Proc.HasExited) { try { Stop-Process -Id $Proc.Id -Force } catch {} }
    # the venv's python.exe starts the real interpreter as a child with the same command line
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*aero.server*port=$BasePort*" } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
    # llama-server processes this copy started (its ports only); Aero's Job Object normally ends them already
    $ports = ($BasePort + 1)..($BasePort + 4) + ($BasePort + 13)
    Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $cl = $_.CommandLine; $hit = $false
        foreach ($p in $ports) { if ($cl -match "--port\s+$p\b") { $hit = $true } }
        $hit
    } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
    Write-Host "  Throwaway backend stopped."
}

# ------------------------------------------------------------------------------------------------ was anything changed?

$After = Fingerprint
$changed = @($Before.Keys | Where-Object { "$($Before[$_])" -ne "$($After[$_])" })
if ($changed.Count -eq 0) { Add-Check "Your real install was not changed" "pass" "settings, models, tunings, memory, chats and model files identical before and after" }
else { Add-Check "Your real install was not changed" "warn" "changed during the run: $($changed -join ', ') (if your own Aero was open, it may have written these itself)" }

# ------------------------------------------------------------------------------------------------ compare

$Cmp = @()
if ($CompareTo -and (Test-Path -LiteralPath $CompareTo)) {
    $old = Get-Content -LiteralPath $CompareTo -Raw | ConvertFrom-Json
    foreach ($k in @($R.metrics.Keys)) {
        $nv = $R.metrics[$k]
        if (-not ($nv -is [double] -or $nv -is [int] -or $nv -is [long] -or $nv -is [decimal])) { continue }
        if (-not $old.metrics.PSObject.Properties[$k]) { continue }
        $ov = $old.metrics.$k
        $pct = if ($ov) { [math]::Round(($nv - $ov) / $ov * 100, 1) } else { $null }
        $Cmp += [ordered]@{ metric = $k; before = $ov; after = $nv; change_pct = $pct }
    }
}

# ------------------------------------------------------------------------------------------------ report

$R.finished = (Get-Date).ToString("s")
$R.compare = $Cmp
$R | ConvertTo-Json -Depth 30 | Set-Content -Path (Join-Path $RunDir "results.json") -Encoding UTF8

$md = New-Object System.Text.StringBuilder
[void]$md.AppendLine("# Aero validation report")
[void]$md.AppendLine("")
[void]$md.AppendLine("Run $Stamp on $($R.machine.os). Aero $($R.machine.aero), llama.cpp $($R.machine.llama).")
[void]$md.AppendLine("CPU: $($R.machine.cpu). RAM: $($R.machine.ram_gb) GB.")
foreach ($g in $winGpus) { [void]$md.AppendLine("GPU: $($g.name), $($g.vram_mb) MB, driver $($g.driver).") }
[void]$md.AppendLine("")
[void]$md.AppendLine("## Checks")
[void]$md.AppendLine("")
[void]$md.AppendLine("| Check | Result | Detail |")
[void]$md.AppendLine("|---|---|---|")
foreach ($c in $R.checks) { [void]$md.AppendLine("| $($c.name) | $($c.status) | $(($c.detail -replace '\|', '/') -replace "`r?`n", ' ') |") }
[void]$md.AppendLine("")
[void]$md.AppendLine("## Measured")
[void]$md.AppendLine("")
foreach ($k in $R.metrics.Keys) { if ($k -notin @("bench", "router")) { [void]$md.AppendLine("- $k : $($R.metrics[$k])") } }
if ($R.metrics.Contains("bench")) { [void]$md.AppendLine("- Benchmark details: bench-report.md and results.json") }
if ($Cmp.Count) {
    [void]$md.AppendLine("")
    [void]$md.AppendLine("## Compared with $CompareTo")
    [void]$md.AppendLine("")
    [void]$md.AppendLine("| Metric | Before | After | Change |")
    [void]$md.AppendLine("|---|---|---|---|")
    foreach ($c in $Cmp) { [void]$md.AppendLine("| $($c.metric) | $($c.before) | $($c.after) | $($c.change_pct)% |") }
}
if ($R.notes.Count) { [void]$md.AppendLine(""); [void]$md.AppendLine("## Notes"); [void]$md.AppendLine(""); foreach ($n in $R.notes) { [void]$md.AppendLine("- $n") } }
[void]$md.AppendLine("")
[void]$md.AppendLine("## Needs a person")
[void]$md.AppendLine("")
foreach ($p in $R.person) { [void]$md.AppendLine("- [ ] $p") }
Set-Content -Path (Join-Path $RunDir "REPORT.md") -Value $md.ToString() -Encoding UTF8

$fails = @($R.checks | Where-Object { $_.status -eq "fail" }).Count
$warns = @($R.checks | Where-Object { $_.status -eq "warn" }).Count
Section "Done"
Write-Host ("  {0} checks: {1} failed, {2} warnings." -f $R.checks.Count, $fails, $warns) -ForegroundColor $(if ($fails) { "Red" } elseif ($warns) { "Yellow" } else { "Green" })
Write-Host "  Report: $(Join-Path $RunDir 'REPORT.md')"
Write-Host "  Data:   $(Join-Path $RunDir 'results.json')"
Write-Host "  The throwaway folder can be deleted any time: $RunDir"
if ($fails) { exit 1 } else { exit 0 }
