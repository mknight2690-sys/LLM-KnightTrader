# Create Start / Stop shortcuts on the OneDrive Desktop when available,
# otherwise fall back to the normal Desktop folder.
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LauncherDir = Join-Path $ProjectRoot "launcher"
$desktopCandidates = @(
    $env:OneDrive,
    [Environment]::GetFolderPath("Desktop")
) | Where-Object { $_ -and (Test-Path $_) }
if (-not $desktopCandidates) {
    throw "Desktop location not found."
}
$desktop = Join-Path $desktopCandidates[0] "Desktop"
if (-not (Test-Path $desktop)) {
    $desktop = $desktopCandidates[0]
}

$shortcuts = @(
    @{
        Name = "LLM KnightTrader Start.lnk"
        Target = Join-Path $LauncherDir "Start LLM KnightTrader.bat"
        Description = "Start LLM KnightTrader trading stack"
    },
    @{
        Name = "LLM KnightTrader Stop.lnk"
        Target = Join-Path $LauncherDir "Stop LLM KnightTrader.bat"
        Description = "Stop LLM KnightTrader trading stack"
    }
)

$shell = New-Object -ComObject WScript.Shell
foreach ($item in $shortcuts) {
    $path = Join-Path $desktop $item.Name
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $item.Target
    $lnk.WorkingDirectory = $ProjectRoot
    $lnk.WindowStyle = 1
    $lnk.Description = $item.Description
    $lnk.Save()
    Write-Host "Created $path"
}
