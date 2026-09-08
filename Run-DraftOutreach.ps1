# Preserve Python's real exit status for Task Scheduler.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
$env:PYTHONIOENCODING = 'utf-8'
$logFolder = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Force -Path $logFolder | Out-Null
$logPath = Join-Path $logFolder 'draft-outreach.log'
python run.py draft-outreach 2>&1 | ForEach-Object {
    Write-Host $_
    Add-Content -LiteralPath $logPath -Value ([string]$_) -Encoding UTF8
}
exit $LASTEXITCODE
