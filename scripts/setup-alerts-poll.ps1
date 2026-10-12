# Registers a Windows scheduled task that runs scripts/alerts-poll.ps1 repeatedly through the day (default every 30 minutes, 06:00-23:00).
# It reads your inbox (read-only) for LinkedIn job alerts, opens only jobs it has not seen, queues them and drafts notes. It never sends or submits.
#
#   ./scripts/setup-alerts-poll.ps1                  # every 30 minutes
#   ./scripts/setup-alerts-poll.ps1 -Minutes 60      # every hour
#   ./scripts/setup-alerts-poll.ps1 -Remove          # uninstall
#
# The laptop must be on and you signed in; runs missed while it was off are skipped (the daily 09:00 run still covers the day).
param([int]$Minutes = 30, [switch]$Remove)
$ErrorActionPreference = 'Stop'
$name = 'JobLinkAlerts'
if ($Remove) { Unregister-ScheduledTask -TaskName $name -Confirm:$false; Write-Host "Removed $name"; return }

$project = Split-Path $PSScriptRoot -Parent
$script = Join-Path $PSScriptRoot 'alerts-poll.ps1'
$log = Join-Path $project 'logs\alerts-poll.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`" >> `"$log`" 2>&1"
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$command`"" -WorkingDirectory $project
# A daily trigger that repeats every N minutes for 17 hours (the script itself also skips quiet hours).
$trigger = New-ScheduledTaskTrigger -Daily -At '06:00'
$repeat = New-ScheduledTaskTrigger -Once -At '06:00' -RepetitionInterval (New-TimeSpan -Minutes $Minutes) -RepetitionDuration (New-TimeSpan -Hours 17)
$trigger.Repetition = $repeat.Repetition
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RunOnlyIfNetworkAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 40)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Description 'Job Link: read LinkedIn job-alert emails, queue the new jobs. Never sends or submits anything.' -Force | Out-Null
Write-Host "Installed ${name}: every $Minutes minutes from 06:00 to 23:00. Output goes to $log"
Write-Host 'Test it now with:  ./scripts/alerts-poll.ps1'
