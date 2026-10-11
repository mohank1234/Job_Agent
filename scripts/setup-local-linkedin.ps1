# Registers a Windows scheduled task that runs scripts/local-linkedin.ps1 every morning, shortly after the cloud run.
# It is your decision to install it: the task runs unattended, drives your logged-in LinkedIn browser profile,
# and publishes the encrypted database to your private repository.
#
#   ./scripts/setup-local-linkedin.ps1                 # daily at 09:00
#   ./scripts/setup-local-linkedin.ps1 -Time 08:30
#   ./scripts/setup-local-linkedin.ps1 -Remove         # uninstall
#
# The laptop must be on and you must be signed in: if it was off at the scheduled time, Windows runs the task as soon as it can.
param([string]$Time = '09:00', [switch]$Remove)
$ErrorActionPreference = 'Stop'
$name = 'JobLinkLinkedIn'
if ($Remove) { Unregister-ScheduledTask -TaskName $name -Confirm:$false; Write-Host "Removed $name"; return }

$project = Split-Path $PSScriptRoot -Parent
$script = Join-Path $PSScriptRoot 'local-linkedin.ps1'
$log = Join-Path $project 'logs\local-linkedin.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`" >> `"$log`" 2>&1"
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$command`"" -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# Run on battery, wait for a network connection (a task fired right after waking would otherwise fail), and catch up if the laptop was off.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RunOnlyIfNetworkAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Description 'Job Link: LinkedIn discovery and hiring-contact search from this machine, then publishes state. Never submits or sends anything.' -Force | Out-Null
Write-Host "Installed ${name}: daily at $Time. Output goes to $log"
Write-Host 'Test it now with:  ./scripts/local-linkedin.ps1'
