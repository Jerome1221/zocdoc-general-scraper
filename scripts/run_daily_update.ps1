param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace,
    [string]$PackageRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DatabaseUrl = ""
)

$ErrorActionPreference = "Stop"
$package = (Resolve-Path -LiteralPath $PackageRoot).Path
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$collector = Join-Path $package ".venv\Scripts\zocdoc-collector.exe"
if (-not (Test-Path -LiteralPath $collector)) {
    throw "Package virtual environment not found: $collector"
}

$logDirectory = Join-Path $workspacePath "logs\daily-updater"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logDirectory "daily-$stamp.log"

if ($DatabaseUrl) {
    $env:ZOCDOC_DATABASE_URL = $DatabaseUrl
}

& $collector --workspace $workspacePath runners start *>&1 |
    Tee-Object -FilePath $logPath
$runnerExit = $LASTEXITCODE
if ($runnerExit -ne 0) {
    exit $runnerExit
}

& $collector --workspace $workspacePath updater daily --live *>&1 |
    Tee-Object -FilePath $logPath -Append
$dailyExit = $LASTEXITCODE
exit $dailyExit

