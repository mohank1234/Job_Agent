# Daily 06:00 IST search and outreach drafting; retries/logon only before 11:00.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = (& python -c "import sys; print(sys.executable)").Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonExe)) { throw 'Cannot resolve the Python interpreter.' }
$userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$istZone = [System.TimeZoneInfo]::FindSystemTimeZoneById('India Standard Time')
$istDate = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $istZone).ToString('yyyy-MM-dd')
$startBoundary = $istDate + 'T06:00:00+05:30'
$escape = { param($value) [System.Security.SecurityElement]::Escape([string]$value) }
$escapedRoot = & $escape $projectRoot
$arguments = & $escape "-NoProfile -WindowStyle Hidden -File `"$projectRoot\Run-Report.ps1`" -PythonExe `"$pythonExe`""
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Search employer boards and prepare unsent outreach once per IST date, only 06:00-11:00; publish into the existing Drive folder. Approval required before sending.</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Repetition><Interval>PT15M</Interval><Duration>PT5H</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>$startBoundary</StartBoundary><Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <LogonTrigger><Enabled>true</Enabled><UserId>$userSid</UserId></LogonTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>$userSid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT5H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author"><Exec><Command>powershell.exe</Command><Arguments>$arguments</Arguments><WorkingDirectory>$escapedRoot</WorkingDirectory></Exec></Actions>
</Task>
"@
Register-ScheduledTask -TaskName 'JobAgent Verified Report' -Xml $xml -Force | Out-Null
$oldOutreach = Get-ScheduledTask -TaskName 'JobAgent Draft Outreach' -ErrorAction SilentlyContinue
if ($oldOutreach) { $oldOutreach | Disable-ScheduledTask | Out-Null }
Write-Host 'Installed: 06:00 IST daily, catch-up checks every 15 minutes until 11:00 and at logon. Python enforces the window and deduplicates completed runs. Outreach stays unsent.'
