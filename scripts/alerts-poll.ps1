# The frequent check (installed by scripts/setup-alerts-poll.ps1, every 30 minutes by default):
#   1. reads new LinkedIn job-alert emails from your inbox (read-only, app password, last 24 hours)
#   2. opens only the alerted jobs the database has not seen yet (paced and counted like every LinkedIn page)
#   3. if any are new and eligible: queues them, fills their forms up to Submit, and drafts notes for the hiring-team person
#   4. then runs the full scripts/local-linkedin.ps1 (LinkedIn search, scoring, application prep, company people search, state publishing),
#      but at most once every -FullEveryMinutes (default 60): a full run takes 25-50 minutes and drives your LinkedIn session the whole time,
#      so running it back to back around the clock would be the kind of continuous traffic LinkedIn restricts accounts for.
# It never sends an invitation and never submits an application.
param([int]$MaxJobs = 15, [int]$FromHour = 6, [int]$ToHour = 23, [int]$FullEveryMinutes = 60)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$stamp = { Get-Date -Format s }

# Quiet hours, and never collide with another Job Link process (a full run, the daily run, or you).
$hour = (Get-Date).Hour
if ($hour -lt $FromHour -or $hour -ge $ToHour) { return }
if (Test-Path 'data/run.lock') {
  $lock = Get-Content 'data/run.lock' -Raw | ConvertFrom-Json
  if (Get-Process -Id $lock.pid -ErrorAction SilentlyContinue) { Write-Host "$(& $stamp) another Job Link process is running; skipping this check"; return }
}
$env:JOBAGENT_MAIL_PASSWORD = [Environment]::GetEnvironmentVariable('JOBAGENT_MAIL_PASSWORD', 'User')
$env:VAULT_PASSPHRASE = [Environment]::GetEnvironmentVariable('VAULT_PASSPHRASE', 'User')

function Read-AlertJobs {
  if (-not $env:JOBAGENT_MAIL_PASSWORD) { Write-Host "$(& $stamp) JOBAGENT_MAIL_PASSWORD is not set; skipping the alert emails"; return }
  Push-Location agent
  try { python run.py mailbox-export --out ../data/alert-jobs.json --days 1; $mailCode = $LASTEXITCODE } finally { Pop-Location }
  if ($mailCode -ne 0) { Write-Warning "$(& $stamp) could not read the alert emails (app password revoked?)"; return }
  npx tsx src/cli.ts import-alerts data/alert-jobs.json --headless --limit $MaxJobs 2>&1 | Tee-Object -Variable captured | Out-Null
  $line = ($captured | Select-String -Pattern 'new=(\d+).*eligible=(\d+)' | Select-Object -Last 1)
  if (-not $line) { Write-Host "$(& $stamp) import produced no summary"; return }
  $new = [int]$line.Matches[0].Groups[1].Value; $eligible = [int]$line.Matches[0].Groups[2].Value
  Write-Host "$(& $stamp) alerted jobs: $new new, $eligible eligible"
  # Queue, fill forms up to Submit, and draft notes for the new eligible jobs only (no LinkedIn search in this step).
  if ($eligible -gt 0) { npx tsx src/cli.ts run --headless --no-discover --limit 10 }
}
Read-AlertJobs

$marker = 'data/last-full-run.txt'
$last = if (Test-Path $marker) { [datetime](Get-Content $marker -Raw).Trim() } else { [datetime]::MinValue }
if (((Get-Date) - $last).TotalMinutes -ge $FullEveryMinutes) {
  Write-Host "$(& $stamp) starting the full LinkedIn run (last one: $(if ($last -eq [datetime]::MinValue) { 'never' } else { $last.ToString('s') }))"
  # Mark first, so a run that is interrupted or fails is not retried every 30 minutes around the clock.
  (Get-Date).ToString('s') | Set-Content $marker
  & "$PSScriptRoot/local-linkedin.ps1"
} else { Write-Host "$(& $stamp) full run not due yet (every $FullEveryMinutes min)" }
