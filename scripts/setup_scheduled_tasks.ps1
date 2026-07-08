# scripts/setup_scheduled_tasks.ps1 -run as administrator
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found at $python -run 'python -m venv .venv' and install dependencies first." }

function New-YnabTask {
    param([string]$Name, [string]$Script, [int]$IntervalMinutes)
    $action = New-ScheduledTaskAction -Execute $python -Argument "-m bot.$Script" -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
    Write-Host "Registered: $Name (every $IntervalMinutes min)"
}

# Phase 7+ (2026-06-26): YnabWatcher removed (bot/ynab_watcher.py deleted)
# and SampleCollector disabled (parsers are built; samples no longer
# needed). GmailWatcher external task superseded by the bot's in-process
# 60s gmail poll loop. Kept commented for re-provisioning reference:
# New-YnabTask -Name "YNAB-Helper-GmailWatcher"    -Script "gmail_watcher"    -IntervalMinutes 5
# New-YnabTask -Name "YNAB-Helper-SampleCollector" -Script "sample_collector" -IntervalMinutes 60

# Daily catch-up: refreshes LLM suggestions on pending_txn rows so the bot has
# things to DM. Runs at 7:30am, just before quiet hours end at 7:00am... actually
# right after, so first morning DM lands while the user is checking their phone.
$catchupAction = New-ScheduledTaskAction -Execute $python -Argument "-m scripts.daily_catchup --limit 20" -WorkingDirectory $RepoRoot
$catchupTrigger = New-ScheduledTaskTrigger -Daily -At 7:30am
$catchupSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName "YNAB-Helper-DailyCatchup" -Action $catchupAction -Trigger $catchupTrigger -Settings $catchupSettings -RunLevel Highest -Force | Out-Null
Write-Host "Registered: YNAB-Helper-DailyCatchup (daily 7:30am)"

# DB backup: consistent SQLite snapshot pushed to Google Drive every 6h.
# The live DB stays on LOCAL disk (Drive corrupts live SQLite); this task
# is how Drive gets its copies. Runs as Limited so it needs no elevation.
$backupAction = New-ScheduledTaskAction -Execute $python -Argument "-m scripts.backup_db --keep 28" -WorkingDirectory $RepoRoot
$backupTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) -RepetitionInterval (New-TimeSpan -Hours 6)
$backupSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "YNAB-Helper-Backup" -Action $backupAction -Trigger $backupTrigger -Settings $backupSettings -Force | Out-Null
Write-Host "Registered: YNAB-Helper-Backup (every 6h, keep 28 snapshots)"

# Long-running bot -separate task, runs at logon, restarts on failure
$action = New-ScheduledTaskAction -Execute $python -Argument "-m bot.telegram_bot" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 9999)
Register-ScheduledTask -TaskName "YNAB-Helper-Bot" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
Write-Host "Registered: YNAB-Helper-Bot (long-running)"

Write-Host "`nDone. Manage tasks via Task Scheduler GUI (taskschd.msc) under 'Task Scheduler Library'."
