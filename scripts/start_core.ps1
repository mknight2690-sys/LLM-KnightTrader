# Desktop Start — stop entire stack, then start dashboard + trader.
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

$args = @("start")
if ($OpenBrowser) { $args += "--open-browser" }

& $Python (Join-Path $PSScriptRoot "stack_launcher.py") @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
