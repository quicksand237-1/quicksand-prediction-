param(
    [string]$Python = "C:\Study program\Python\python.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }

$probe = & $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Install it in the selected Python environment, then rerun this script."
}
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name AIPRP-DashboardLauncher `
    --distpath (Join-Path $root "dist_dashboard_launcher") `
    --workpath (Join-Path $root "build_dashboard_launcher") `
    --add-data "dashboard_launcher.html;." `
    dashboard_launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
Write-Host "Built: $(Join-Path $root 'dist_dashboard_launcher\AIPRP-DashboardLauncher.exe')" -ForegroundColor Green
