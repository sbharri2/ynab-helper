# Fix the YNAB-Helper-Bot scheduled task so it self-recovers.
#
# Symptom: the task's trigger is LogonTrigger only. When the bot process
# exits (it died 2026-06-10 with code 1), it doesn't come back until next
# logon — and Steven stays logged in for weeks at a time.
#
# Fix: replace the trigger with two: AtLogon (covers cold start) AND a
# repetition that fires every 5 minutes. Scheduled Tasks defaults to
# "do not start new instance if already running", so 5-min repetition is
# a no-op when the bot is healthy and a fast recovery when it crashes.
#
# Run from an elevated PowerShell.

$ErrorActionPreference = "Stop"

$taskName = "YNAB-Helper-Bot"
$pyExe = "C:\Users\Steven\ynabhelper\.venv\Scripts\python.exe"
$workDir = "C:\Users\Steven\ynabhelper"

$action = New-ScheduledTaskAction `
    -Execute $pyExe `
    -Argument "-m bot.telegram_bot" `
    -WorkingDirectory $workDir

# AtLogon for cold start (covers boot/login)
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$triggerLogon.Repetition = (New-CimInstance -ClassName MSFT_TaskRepetitionPattern `
    -Namespace Root/Microsoft/Windows/TaskScheduler `
    -ClientOnly `
    -Property @{
        Interval = "PT5M"
        Duration = ""    # empty = forever
    })

# Once-a-day belt-and-suspenders trigger (in case Logon trigger somehow
# misses; the duplicate is a no-op because instance policy is single-run).
$triggerDaily = New-ScheduledTaskTrigger -Daily -At 7:00am

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -DontStopOnIdleEnd

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($triggerLogon, $triggerDaily) `
    -Settings $settings `
    -RunLevel Highest `
    -Force

Write-Host "Registered $taskName"
Get-ScheduledTask -TaskName $taskName | Format-List TaskName, State
Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo | Format-List LastRunTime, LastTaskResult, NextRunTime
