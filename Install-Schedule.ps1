# Installs the Job Agent as a Windows scheduled task.
#
# Runs every morning at 10:30. If that run is missed or fails, Watch picks the
# day up the moment logon or a network connect makes it possible, and Retry at
# 11:00 is the backstop for a day nothing else caught. Catchup at 20:00 skips
# the fetch and only works the scoring backlog. All four run on battery and
# catch up via StartWhenAvailable if the machine was off; none wake it from
# sleep.
#
#   powershell -ExecutionPolicy Bypass -File D:\job-agent\Install-Schedule.ps1
#
# Remove them again with:
#   Unregister-ScheduledTask -TaskName "JobAgent Daily","JobAgent Watch","JobAgent Retry","JobAgent Catchup" -Confirm:$false

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "Run-Daily.ps1"

if (-not (Test-Path $script)) {
    Write-Error "Run-Daily.ps1 not found next to this script"
    exit 1
}

# Register-ScheduledTask reports a refusal as a NON-TERMINATING error, so with
# the default preference the script sails past it and prints "Installed" for a
# task that does not exist. Anything that cannot be registered must be loud.
$ErrorActionPreference = "Stop"

# AllowStartIfOnBatteries / DontStopIfGoingOnBatteries are not cosmetic. Task
# Scheduler defaults both the other way, and on a laptop that is not plugged in
# the run does not fail - it sits in state Queued forever and the log simply
# never gains a line. They were fixed by hand on the live tasks once already;
# without them here, every re-run of this installer puts the bug back.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew

# --- morning: fetch + score -------------------------------------------------
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -ScoreMinutes 90" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 10:30am
Register-ScheduledTask -TaskName "JobAgent Daily" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Fetch jobs, classify, score, write digest" -Force |
    Out-Null
Write-Host "Installed: JobAgent Daily      10:30  fetch + up to 90 min scoring"

# --- retry: only if the 10:30 pass did not produce a digest -----------------
# -OnlyIfFailed makes this a no-op on a normal day: it exits immediately if the
# 10:30 task is still running or already wrote logs\last-success.txt, so the
# cost of the retry existing is one hidden PowerShell start.
$catchupArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -ScoreMinutes 90 -OnlyIfFailed -NotBefore 10:30"

$actionR = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument $catchupArgs -WorkingDirectory $root
$triggerR = New-ScheduledTaskTrigger -Daily -At 11:00am
Register-ScheduledTask -TaskName "JobAgent Retry" -Action $actionR -Trigger $triggerR `
    -Settings $settings -Description "Backstop: re-run the day at 11:00 if it is still uncovered" -Force |
    Out-Null
Write-Host "Installed: JobAgent Retry      11:00  backstop if the day is still uncovered"

# --- watch: pick a missed run up the moment the machine or link returns -----
# StartWhenAvailable already re-runs Daily after the machine was off, but it
# says nothing about the far more common case here: the machine is awake and
# the internet is not. So this task fires on logon and on every
# network-connect event, and -OnlyIfFailed makes it a no-op unless the day is
# genuinely uncovered. Waiting for 11:00 was the gap this closes.
#
# Event triggers cannot be expressed with New-ScheduledTaskTrigger, so the
# NetworkProfile subscription has to be built as a raw CIM instance.
$actionW = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument $catchupArgs -WorkingDirectory $root

# Logon, delayed 3 minutes: at logon the network stack is usually still coming
# up, and Test-Online would fail on a link that works 30 seconds later.
#
# -User is required, not cosmetic. Without it this is an ANY-user logon
# trigger, which only an administrator may register, and the whole task is
# refused with "Access is denied".
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$triggerLogon.Delay = "PT3M"

# NetworkProfile/Operational 10000 = a network became connected. Fires on wifi
# reconnects, cable plug-in and VPN up. It can fire several times while a link
# flaps; the run.lock in Run-Daily.ps1 is what makes that safe.
$cimTrigger = Get-CimClass -ClassName MSFT_TaskEventTrigger `
    -Namespace Root/Microsoft/Windows/TaskScheduler
$triggerNet = New-CimInstance -CimClass $cimTrigger -ClientOnly
$triggerNet.Enabled = $true
$triggerNet.Subscription = @"
<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational">
<Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[Provider[@Name='Microsoft-Windows-NetworkProfile'] and EventID=10000]]</Select>
</Query></QueryList>
"@
# Let the link settle before the connectivity probe runs.
$triggerNet.Delay = "PT1M"

Register-ScheduledTask -TaskName "JobAgent Watch" -Action $actionW `
    -Trigger @($triggerLogon, $triggerNet) `
    -Settings $settings -Description "Run a missed day as soon as logon or a network connect allows" -Force |
    Out-Null
Write-Host "Installed: JobAgent Watch      on logon + network connect (no-op unless uncovered)"

# --- evening: scoring backlog only -----------------------------------------
$action2 = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -SkipFetch -ScoreMinutes 120" `
    -WorkingDirectory $root
$trigger2 = New-ScheduledTaskTrigger -Daily -At 8:00pm
Register-ScheduledTask -TaskName "JobAgent Catchup" -Action $action2 -Trigger $trigger2 `
    -Settings $settings -Description "Work the LLM scoring backlog" -Force | Out-Null
Write-Host "Installed: JobAgent Catchup    20:00  scoring backlog only"

Write-Host ""
Write-Host "Check them with:  Get-ScheduledTask -TaskName 'JobAgent*'"
Write-Host "Run one now with: Start-ScheduledTask -TaskName 'JobAgent Daily'"
Write-Host "Logs land in:     $root\logs\"
