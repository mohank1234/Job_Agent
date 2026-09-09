# One daily verified-report refresh; no paid search or LLM calls by default.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = (& python -c "import sys; print(sys.executable)").Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonExe)) { throw 'Cannot resolve the Python interpreter.' }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -File `"$projectRoot\Run-Report.ps1`" -PythonExe `"$pythonExe`"" `
    -WorkingDirectory $projectRoot
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
$nextRun = (Get-Date).Date.AddHours(16)
if ($nextRun -le (Get-Date)) { $nextRun = $nextRun.AddDays(1) }
$trigger = New-ScheduledTaskTrigger -Daily -At $nextRun
Register-ScheduledTask -TaskName 'JobAgent Verified Report' -Action $action `
    -Settings $settings -Trigger $trigger `
    -Description 'Fetch current full JDs and update the permanent JobAgent Drive report; no outbound communications.' -Force | Out-Null
Write-Host 'Installed: JobAgent Verified Report, daily at 4:00pm local time.'
