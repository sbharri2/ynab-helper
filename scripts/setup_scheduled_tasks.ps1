# scripts/setup_scheduled_tasks.ps1 — run as administrator
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found at $python — run 'python -m venv .venv' and install dependencies first." }

function New-YnabTask {
    param([string]$Name, [string]$Script, [int]$IntervalMinutes)
    $action = New-ScheduledTaskAction -Execute $python -Argument "-m bot.$Script" -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
    Write-Host "Registered: $Name (every $IntervalMinutes min)"
}

New-YnabTask -Name "YNAB-Helper-GmailWatcher" -Script "gmail_watcher" -IntervalMinutes 5
New-YnabTask -Name "YNAB-Helper-YnabWatcher"  -Script "ynab_watcher"  -IntervalMinutes 30

# Long-running bot — separate task, runs at logon, restarts on failure
$action = New-ScheduledTaskAction -Execute $python -Argument "-m bot.telegram_bot" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 9999)
Register-ScheduledTask -TaskName "YNAB-Helper-Bot" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Write-Host "Registered: YNAB-Helper-Bot (long-running)"

Write-Host "`nDone. Manage tasks via Task Scheduler GUI (taskschd.msc) under 'Task Scheduler Library'."
