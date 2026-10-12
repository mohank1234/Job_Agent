# The light, frequent check (installed by scripts/setup-alerts-poll.ps1, every 30 minutes by default):
#   1. reads new LinkedIn job-alert emails from your inbox (read-only, app password, last 24 hours)
#   2. opens only the alerted jobs the database has not seen yet (paced and counted like every LinkedIn page)
#   3. if any are new and eligible: queues them, fills their forms up to Submit, and drafts notes for the hiring-team person
# It never sends an invitation and never submits an application. The daily scripts/local-linkedin.ps1 (09:00) still does the full search,
# the company people search and the state publishing; this script leaves the shared state alone.
param([int]$MaxJobs = 15, [int]$FromHour = 6, [int]$ToHour = 23)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$stamp = { Get-Date -Format s }

# Quiet hours, and never collide with another Job Link process (the daily run, or you).
$hour = (Get-Date).Hour
if ($hour -lt $FromHour -or $hour -ge $ToHour) { return }
if (Test-Path 'data/run.lock') {
  $lock = Get-Content 'data/run.lock' -Raw | ConvertFrom-Json
  if (Get-Process -Id $lock.pid -ErrorAction SilentlyContinue) { Write-Host "$(& $stamp) another Job Link process is running; skipping this check"; return }
}
$env:JOBAGENT_MAIL_PASSWORD = [Environment]::GetEnvironmentVariable('JOBAGENT_MAIL_PASSWORD', 'User')
if (-not $env:JOBAGENT_MAIL_PASSWORD) { Write-Host "$(& $stamp) JOBAGENT_MAIL_PASSWORD is not set; nothing to do"; return }
$env:VAULT_PASSPHRASE = [Environment]::GetEnvironmentVariable('VAULT_PASSPHRASE', 'User')

Push-Location agent
try { python run.py mailbox-export --out ../data/alert-jobs.json --days 1; $mailCode = $LASTEXITCODE } finally { Pop-Location }
if ($mailCode -ne 0) { Write-Warning "$(& $stamp) could not read the alert emails (app password revoked?)"; return }

$importOutput = npx tsx src/cli.ts import-alerts data/alert-jobs.json --headless --limit $MaxJobs 2>&1 | Tee-Object -Variable captured
$line = ($captured | Select-String -Pattern 'new=(\d+).*eligible=(\d+)' | Select-Object -Last 1)
if (-not $line) { Write-Host "$(& $stamp) import produced no summary"; return }
$new = [int]$line.Matches[0].Groups[1].Value; $eligible = [int]$line.Matches[0].Groups[2].Value
Write-Host "$(& $stamp) alerted jobs: $new new, $eligible eligible"
if ($eligible -gt 0) {
  # Queue, fill forms up to Submit, and draft notes for the new eligible jobs only (no LinkedIn search this time).
  npx tsx src/cli.ts run --headless --no-discover --limit 10
}
