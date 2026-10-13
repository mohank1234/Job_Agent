# The frequent check (installed by scripts/setup-alerts-poll.ps1, every 30 minutes by default). It is light on purpose:
#   1. reads new LinkedIn job-alert emails from your inbox (read-only, app password, last 24 hours)
#   2. opens only the alerted jobs the database has not seen yet (paced and counted like every LinkedIn page); a check with nothing new opens no page
#   3. only when new eligible jobs arrived: queues them, fills their forms up to Submit, drafts notes for the hiring-team person, and searches
#      for people at those companies (at most 5 companies)
# Jobs from your alert emails are treated as eligible (your own rules still apply: no internships, nothing under the minimum experience).
# There is no broad LinkedIn search any more. It never sends an invitation and never submits an application.
# The daily scripts/local-linkedin.ps1 (09:00) still publishes the shared state.
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
$env:VAULT_PASSPHRASE = [Environment]::GetEnvironmentVariable('VAULT_PASSPHRASE', 'User')
if (-not $env:JOBAGENT_MAIL_PASSWORD) { Write-Host "$(& $stamp) JOBAGENT_MAIL_PASSWORD is not set; nothing to do"; return }

Push-Location agent
try { python run.py mailbox-export --out ../data/alert-jobs.json --days 1; $mailCode = $LASTEXITCODE } finally { Pop-Location }
if ($mailCode -ne 0) { Write-Warning "$(& $stamp) could not read the alert emails (app password revoked?)"; return }

npx tsx src/cli.ts import-alerts data/alert-jobs.json --headless --limit $MaxJobs 2>&1 | Tee-Object -Variable captured | Out-Null
$line = ($captured | Select-String -Pattern 'new=(\d+).*eligible=(\d+)' | Select-Object -Last 1)
if (-not $line) { Write-Host "$(& $stamp) import produced no summary"; return }
$new = [int]$line.Matches[0].Groups[1].Value; $eligible = [int]$line.Matches[0].Groups[2].Value
Write-Host "$(& $stamp) alerted jobs: $new new, $eligible eligible"
if ($eligible -gt 0) {
  npx tsx src/cli.ts run --headless --no-discover --limit 10
  npx tsx src/cli.ts find-contacts --headless --companies 5
}
