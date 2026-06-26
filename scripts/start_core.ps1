# Kill all KnightTrader agents, then start exactly one dashboard + one trader.
param(
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = $env:KNIGHTTRADER_PYTHON
if (-not $Python -or -not (Test-Path $Python)) {
    $pyCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd) { $Python = $pyCmd.Source }
}
if (-not $Python -or -not (Test-Path $Python)) {
    throw "Python not found. Install Python 3.12+ or set KNIGHTTRADER_PYTHON to python.exe path."
}
$PidDir = Join-Path $ProjectRoot "data\pids"
$LogDir = Join-Path $ProjectRoot "data\logs"
$DashboardUrl = "http://127.0.0.1:8765"

New-Item -ItemType Directory -Force -Path $PidDir, $LogDir | Out-Null

function Stop-ByPidFile($name) {
    $path = Join-Path $PidDir "$name.pid"
    if (-not (Test-Path $path)) { return }
    $procId = Get-Content $path -ErrorAction SilentlyContinue
    if ($procId) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $path -Force -ErrorAction SilentlyContinue
}

function Get-AgentCount([string]$fragment) {
    return @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "python.exe" -and $_.CommandLine -match $fragment
    }).Count
}

function Wait-DashboardHealth([int]$MaxSec = 30) {
    for ($i = 0; $i -lt $MaxSec; $i++) {
        try {
            $r = Invoke-WebRequest -Uri "$DashboardUrl/api/health" -TimeoutSec 2 -UseBasicParsing
            if ($r.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Seconds 1
    }
    return $false
}

function Save-Pid($name, $proc) {
    Set-Content -Path (Join-Path $PidDir "$name.pid") -Value $proc.Id
}

function Start-AgentModule([string]$module, [string]$pidName) {
    $proc = Start-Process -FilePath $Python -ArgumentList "-m", $module `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
    if ($proc.HasExited) {
        throw "$module exited immediately (code $($proc.ExitCode))"
    }
    Save-Pid $pidName $proc
    return $proc
}

Write-Host "Stopping any running KnightTrader agents..."
Stop-ByPidFile "monitor"
Stop-ByPidFile "trader"
Stop-ByPidFile "dashboard"
& $Python (Join-Path $PSScriptRoot "kill_agents.py") | Out-Null
Start-Sleep -Seconds 2

$left = (Get-AgentCount "dashboard\.server") + (Get-AgentCount "trader\.agent") + (Get-AgentCount "monitor\.agent")
if ($left -gt 0) {
    & $Python (Join-Path $PSScriptRoot "kill_agents.py") | Out-Null
    Start-Sleep -Seconds 1
}

$dashProc = Start-AgentModule "dashboard.server" "dashboard"
Start-Sleep -Seconds 2
if ($dashProc.HasExited) {
    throw "dashboard.server died during startup"
}

$healthy = Wait-DashboardHealth
if (-not $healthy) {
    throw "dashboard health check failed after start"
}

$traderProc = Start-AgentModule "trader.agent" "trader"
Start-Sleep -Seconds 1
if ($traderProc.HasExited) {
    throw "trader.agent died during startup"
}

$dashCount = Get-AgentCount "dashboard\.server"
$traderCount = Get-AgentCount "trader\.agent"

Write-Host "Dashboard PID $($dashProc.Id) instances=$dashCount health=$healthy"
Write-Host "Trader PID $($traderProc.Id) instances=$traderCount"

if ($dashCount -ne 1 -or $traderCount -ne 1) {
    Write-Host "ERROR: expected single dashboard and trader instance"
    exit 1
}

if ($OpenBrowser) {
    Start-Process $DashboardUrl
}

$shortcutScript = Join-Path $PSScriptRoot "create_desktop_shortcuts.ps1"
if (Test-Path $shortcutScript) {
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $shortcutScript
    } catch {
        Write-Host "Note: desktop shortcuts could not be created: $_"
    }
}

Write-Host "LLM KnightTrader ready -> $DashboardUrl"
