param(
    [string]$Slug = "",
    [string]$Db = "data\extended.sqlite",
    [string]$Model = "data\extended_model_v4.json",
    [string]$Proxy = "http://127.0.0.1:7897",
    [string[]]$ExtraSlug = @(),
    [int]$Port = 8765,
    [int]$RefreshSeconds = 30,
    [int]$Timeout = 20
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Get-ActiveMarketSlugs {
    $timezone = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $now = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $timezone)
    $marketStarts = @()
    for ($offset = 0; $offset -lt 8; $offset += 1) {
        $candidate = $now.Date.AddDays(-$offset)
        $isStartDay = $candidate.DayOfWeek -in @([DayOfWeek]::Tuesday, [DayOfWeek]::Friday)
        $hasOpened = $candidate -lt $now.Date -or $now.TimeOfDay -ge [TimeSpan]::FromHours(12)
        if ($isStartDay -and $hasOpened) {
            $marketStarts += $candidate
        }
    }
    if ($marketStarts.Count -lt 2) {
        throw "Could not determine the two active Tuesday/Friday market windows."
    }
    $culture = [System.Globalization.CultureInfo]::InvariantCulture
    return @($marketStarts | Sort-Object -Descending | Select-Object -First 2 | ForEach-Object {
        $end = $_.AddDays(7)
        "elon-musk-of-tweets-$($_.ToString('MMMM', $culture).ToLowerInvariant())-$($_.Day)-$($end.ToString('MMMM', $culture).ToLowerInvariant())-$($end.Day)"
    })
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
    "--timeout", $Timeout
)
foreach ($extra in $ExtraSlug) {
    $arguments += @("--extra-slug", $extra)
}

& python @arguments
