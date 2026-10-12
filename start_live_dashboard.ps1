param(
    [string]$Slug = "",
    [string]$Db = "data\extended.sqlite",
    [string]$Model = "data\extended_model_v4.json",
    [string]$Proxy = "http://127.0.0.1:7897",
    [string[]]$ExtraSlug = @(),
    [int]$Port = 8765,
    [int]$RefreshSeconds = 300,
    [int]$ResearchRefreshSeconds = 30,
    [int]$Timeout = 20
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$dynamicDiscovery = [string]::IsNullOrWhiteSpace($Slug)

function Get-ActiveMarketSlugs {
    $json = & python .\polymarket_pipeline.py discover-live --timezone "America/New_York" --proxy $Proxy --timeout $Timeout
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        throw "Gamma market discovery failed; live service was not started."
    }
    $result = $json | ConvertFrom-Json
    $slugs = @($result.slugs | Where-Object { $_ })
    if ($slugs.Count -eq 0) {
        throw "Gamma found no active Elon Musk count market; live service was not started."
    }
    return $slugs
}

if (-not $Slug) {
    $activeSlugs = Get-ActiveMarketSlugs
    $Slug = $activeSlugs[0]
    $ExtraSlug = @($activeSlugs | Select-Object -Skip 1) + $ExtraSlug
}

if (-not (Test-Path -LiteralPath $Db)) {
    throw "Database not found: $Db"
}

if (-not (Test-Path -LiteralPath $Model)) {
    throw "Model file not found: $Model"
}

$portOwner = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($portOwner) {
    $owners = @($portOwner | Select-Object -ExpandProperty OwningProcess -Unique)
    throw "Port $Port is already listening (PID: $($owners -join ', ')); a second live service was not started."
}

Write-Host "Starting live dashboard on http://127.0.0.1:$Port/" -ForegroundColor Cyan
Write-Host "Slug: $Slug"
if ($ExtraSlug.Count) {
    Write-Host "Extra slugs: $($ExtraSlug -join ', ')"
}
Write-Host "DB:   $Db"
Write-Host "Model:$Model"

$arguments = @(
    ".\live_dashboard_server.py",
    "--slug", $Slug,
    "--db", $Db,
    "--model", $Model,
    "--proxy", $Proxy,
    "--port", $Port,
    "--refresh-seconds", $RefreshSeconds,
    "--research-refresh-seconds", $ResearchRefreshSeconds,
    "--timeout", $Timeout
)
if ($dynamicDiscovery) {
    $arguments += "--dynamic-discovery"
}
foreach ($extra in $ExtraSlug) {
    $arguments += @("--extra-slug", $extra)
}

& python @arguments
