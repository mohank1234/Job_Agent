# One daily public job search, unsent outreach and verified Drive publication.
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

# The LLM is a local Ollama model. Start the server if it isn't running yet
# (after a reboot the tray app may not have launched before this task fires).
function Test-Ollama { try { Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 5 | Out-Null; $true } catch { $false } }
if (-not (Test-Ollama)) {
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollama) { Start-Process -FilePath $ollama.Source -ArgumentList 'serve' -WindowStyle Hidden }
    foreach ($i in 1..30) { if (Test-Ollama) { break }; Start-Sleep -Seconds 2 }
}
if (-not (Test-Ollama)) { Add-Content -LiteralPath $logPath -Value 'Ollama is not reachable; LLM steps will fall back to deterministic scoring.' -Encoding UTF8 }

$runLog = Join-Path $logFolder 'morning-run.log'
Set-Content -LiteralPath $runLog -Value '' -Encoding UTF8
& $PythonExe run.py morning 2>&1 | ForEach-Object {
    Write-Host $_
    Add-Content -LiteralPath $logPath -Value ([string]$_) -Encoding UTF8
    Add-Content -LiteralPath $runLog -Value ([string]$_) -Encoding UTF8
}
$result = $LASTEXITCODE
# 2 = window_ended: the 11:00 IST deadline hit mid-run; the next trigger resumes.
if ($result -ne 0 -and $result -ne 2) {
    $env:JOBAGENT_RUN_LOG = 'logs/morning-run.log'
    $env:GITHUB_RUN_URL = "local run on $env:COMPUTERNAME, full log: $runLog"
    & $PythonExe tools/send_failure_alert.py
} elseif ($result -eq 0) {
    & $PythonExe tools/send_run_email.py
}
exit $result
