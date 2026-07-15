# Create Start / Stop shortcuts on the Windows desktop.
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LauncherDir = Join-Path $ProjectRoot "launcher"
$Desktop = Join-Path $env:USERPROFILE "OneDrive\Desktop"
if (-not (Test-Path $Desktop)) {
    $Desktop = [Environment]::GetFolderPath("Desktop")
}

$shortcuts = @(
    @{
        Name = "Start LLM KnightTrader.lnk"
        Target = Join-Path $LauncherDir "Start LLM KnightTrader.bat"
        Description = "Start LLM KnightTrader trading stack"
    },
    @{
        Name = "Stop LLM KnightTrader.lnk"
        Target = Join-Path $LauncherDir "Stop LLM KnightTrader.bat"
        Description = "Stop LLM KnightTrader trading stack"
    },
    @{
        Name = "LLM KnightTrader Agent CLI.lnk"
        Target = Join-Path $LauncherDir "Agent CLI.bat"
        Description = "LLM KnightTrader Agent CLI - NVIDIA GLM 5.1 Systems Operator"
    }
)

$shell = New-Object -ComObject WScript.Shell
foreach ($item in $shortcuts) {
    $path = Join-Path $Desktop $item.Name
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $item.Target
    $lnk.WorkingDirectory = $ProjectRoot
    $lnk.WindowStyle = 1
    $lnk.Description = $item.Description
    $lnk.Save()
    Write-Host "Created $path"
}
