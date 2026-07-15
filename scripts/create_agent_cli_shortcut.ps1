# Create Agent CLI shortcut on the Windows desktop.
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LauncherDir = Join-Path $ProjectRoot "launcher"
$Desktop = Join-Path $env:USERPROFILE "OneDrive\Desktop"
if (-not (Test-Path $Desktop)) {
    $Desktop = [Environment]::GetFolderPath("Desktop")
}

$shell = New-Object -ComObject WScript.Shell
$path = Join-Path $Desktop "LLM KnightTrader Agent CLI.lnk"
$lnk = $shell.CreateShortcut($path)
$lnk.TargetPath = Join-Path $LauncherDir "Agent CLI.bat"
$lnk.WorkingDirectory = $ProjectRoot
$lnk.WindowStyle = 1
$lnk.Description = "LLM KnightTrader Agent CLI - NVIDIA GLM 5.1 Systems Operator"
$lnk.IconLocation = "%SystemRoot%\System32\cmd.exe,0"
$lnk.Save()
Write-Host "Created $path"
