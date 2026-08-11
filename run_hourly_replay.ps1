param(
    [string]$Db = "data\extended.sqlite",
    [string]$Start = "2026-02-10",
    [string]$End = "2026-08-09",
    [string]$Report = "data\hourly_extended_v4_fullprob.csv",
    [string]$Summary = "data\hourly_extended_v4_fullprob_summary.csv",
    [string]$PublicReport = "data\hourly_extended_v4_fullprob_public.csv",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path -LiteralPath $Db)) {
    throw "Database not found: $Db"
}

if (-not $Force) {
    foreach ($output in @($Report, $Summary, $PublicReport)) {
        if (Test-Path -LiteralPath $output) {
            throw "Output already exists: $output. Choose another path or pass -Force explicitly."
        }
    }
}

Write-Host "Generating hourly replay CSV files..." -ForegroundColor Cyan
Write-Host "DB:      $Db"
Write-Host "Start:   $Start"
Write-Host "End:     $End"
Write-Host "Report:  $Report"
Write-Host "Summary: $Summary"
Write-Host "Public:  $PublicReport"

& python .\polymarket_pipeline.py hourly-test `
    --db $Db `
    --start $Start `
    --end $End `
    --resolved-bin-labels `
    --report $Report `
    --summary $Summary `
    --public-report $PublicReport
