param(
    [string]$Slug = "elon-musk-of-tweets-august-7-august-14",
    [string]$Db = "data\extended.sqlite",
    [string]$Model = "data\extended_model_v4.json",
    [string]$Proxy = "http://127.0.0.1:7897",
    [int]$Port = 8765,
    [int]$RefreshSeconds = 30,
    [int]$Timeout = 20
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path -LiteralPath $Db)) {
    throw "Database not found: $Db"
}

if (-not (Test-Path -LiteralPath $Model)) {
    throw "Model file not found: $Model"
}

Write-Host "Starting live dashboard on http://127.0.0.1:$Port/" -ForegroundColor Cyan
Write-Host "Slug: $Slug"
Write-Host "DB:   $Db"
Write-Host "Model:$Model"

& python .\live_dashboard_server.py `
    --slug $Slug `
    --db $Db `
    --model $Model `
    --proxy $Proxy `
    --port $Port `
    --refresh-seconds $RefreshSeconds `
    --timeout $Timeout
