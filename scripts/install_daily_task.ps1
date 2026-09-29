param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace,
    [string]$PackageRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$At = "02:00",
    [string]$TaskName = "Zocdoc Provider Daily Sync"
)

$ErrorActionPreference = "Stop"
$package = (Resolve-Path -LiteralPath $PackageRoot).Path
$workspacePath = (Resolve-Path -LiteralPath $Workspace).Path
$runnerScript = Join-Path $package "scripts\run_daily_update.ps1"
$collector = Join-Path $package ".venv\Scripts\zocdoc-collector.exe"
if (-not (Test-Path -LiteralPath $runnerScript)) {
    throw "Daily runner script not found: $runnerScript"
}
if (-not (Test-Path -LiteralPath $collector)) {
    throw "Install the package-local .venv before registering the task: $collector"
}

$time = [datetime]::ParseExact($At, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
$arguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", ('"{0}"' -f $runnerScript),
    "-Workspace", ('"{0}"' -f $workspacePath),
    "-PackageRoot", ('"{0}"' -f $package)
) -join " "

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 23)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Runs the complete Zocdoc provider database synchronization once per day." `
    -Force | Out-Null

Write-Host "Registered task: $TaskName"
Write-Host "Daily time: $At"
Write-Host "Workspace: $workspacePath"
Write-Host "The Windows user must be logged in so managed Chrome can run interactively."
