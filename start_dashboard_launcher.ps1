param(
    [int]$LauncherPort = 8760,
    [int]$DashboardPort = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$python = "C:\Study program\Python\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = "python"
}
$arguments = @(
    ".\dashboard_launcher.py",
    "--launcher-port", $LauncherPort,
    "--dashboard-port", $DashboardPort
)
if ($NoBrowser) {
    $arguments += "--no-browser"
}
& $python @arguments
