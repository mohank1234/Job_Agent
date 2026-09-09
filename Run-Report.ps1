# Refresh the bounded vacancy report and verify its publication to Drive.
# Does not invoke email, draft creation, LLM scoring, or job application commands.
param([string]$PythonExe = 'python')
$ErrorActionPreference = 'Continue'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$logFolder = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Force -Path $logFolder | Out-Null
$logPath = Join-Path $logFolder 'report-refresh.log'
try { Get-Command $PythonExe -ErrorAction Stop | Out-Null } catch { Write-Error 'Configured Python executable is missing.'; exit 1 }
& $PythonExe run.py refresh-report --publish --limit 50 2>&1 | ForEach-Object {
    Write-Host $_
    Add-Content -LiteralPath $logPath -Value ([string]$_) -Encoding UTF8
}
$result = $LASTEXITCODE
exit $result
