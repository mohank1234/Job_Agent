# Installs a narrow Windows scheduled task: just `python run.py draft-outreach`,
# once a day, timed after the cloud "JobAgent Daily Outreach Research" routine
# (runs ~10:00 UTC / 3:30pm IST) typically finishes.
#
# This is deliberately separate from the old JobAgent Daily/Watch/Retry/Catchup
# tasks (removed 2026-09-07) - it does one narrow, idempotent, draft-only thing:
# read the Outreach Report sheet and create Gmail drafts for any row that
# doesn't have one yet (outreach_draft_ledger.json prevents duplicates). It
# never sends anything.
#
#   powershell -ExecutionPolicy Bypass -File D:\job-agent\Install-DraftOutreach-Schedule.ps1
#
# Remove it again with:
#   Unregister-ScheduledTask -TaskName "JobAgent Draft Outreach" -Confirm:$false

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ErrorActionPreference = "Stop"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

$log = Join-Path $root "logs\draft-outreach.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -File `"$root\Run-DraftOutreach.ps1`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 4:15pm

Register-ScheduledTask -TaskName "JobAgent Draft Outreach" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Create Gmail drafts from the Outreach Report sheet (draft-only, never sends)" -Force |
    Out-Null

Write-Host "Installed: JobAgent Draft Outreach   4:15pm daily"
Write-Host "Logs at: $log"
Write-Host "Remove with: Unregister-ScheduledTask -TaskName 'JobAgent Draft Outreach' -Confirm:`$false"
