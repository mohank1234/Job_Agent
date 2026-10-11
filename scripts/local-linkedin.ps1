# The laptop-side fallback for LinkedIn. The cloud run (08:00 IST) does the company boards and, when its LinkedIn session
# is available and accepted, LinkedIn too. If LinkedIn pauses itself in the cloud (HTTP 429 / login wall), this does the
# LinkedIn work from your own connection. The daily page budget is shared, so nothing is fetched twice.
#
#   1. restores the newest state published by the cloud run (a backup of the local database is kept)
#   2. runs LinkedIn discovery, then scores, queues and prepares applications with the shared daily page budget
#   3. searches LinkedIn (read-only) for hiring contacts and drafts connection notes            (skip: -SkipContacts)
#   4. publishes the updated state so the cloud run and your digest see it
#
# Nothing is submitted or sent. Run it by hand or through the scheduled task from scripts/setup-local-linkedin.ps1.
# -Limit is how many applications one run prepares (forms filled, stopping before Submit). Discovery now reads every result LinkedIn shows, so a run
# finds far more jobs than before; raise the limit once you have seen how many you want to review each morning.
param([switch]$SkipContacts, [int]$Limit = 25)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

function Invoke-Checked {
  param([Parameter(Mandatory)][string]$What, [Parameter(Mandatory)][scriptblock]$Command)
  & $Command
  if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)." }
}

if (Test-Path 'data/run.lock') {
  $lock = Get-Content 'data/run.lock' -Raw | ConvertFrom-Json
  if (Get-Process -Id $lock.pid -ErrorAction SilentlyContinue) { throw "Another Job Link process (PID $($lock.pid)) is running; try again when it has finished." }
}
$env:VAULT_PASSPHRASE = [Environment]::GetEnvironmentVariable('VAULT_PASSPHRASE', 'User')
if (-not $env:VAULT_PASSPHRASE) { throw 'The VAULT_PASSPHRASE user environment variable is missing.' }

# Restoring the cloud copy replaces the local database, so anything done on this machine since the last sync (invitations sent, applications
# marked done) is published FIRST. If the cloud is already newer this is refused; invitations already sent are then re-applied from
# data/outreach-ledger.jsonl after the restore, so none is sent twice.
Write-Host "$(Get-Date -Format s) publishing local work before restoring"
npx tsx src/cli.ts state-push
if ($LASTEXITCODE -ne 0) { Write-Warning 'The cloud state is newer, so local work could not be published first; sent invitations are re-applied from the ledger after the restore.' }

Write-Host "$(Get-Date -Format s) restoring the latest state"
Invoke-Checked 'state-pull' { npx tsx src/cli.ts state-pull --force }

Write-Host "$(Get-Date -Format s) LinkedIn discovery, scoring and application preparation"
Invoke-Checked 'run' { npx tsx src/cli.ts run --headless --limit $Limit --source 'LinkedIn configured QA search' }

if (-not $SkipContacts) {
  Write-Host "$(Get-Date -Format s) hiring-contact search"
  npx tsx src/cli.ts find-contacts --headless --companies 10
  if ($LASTEXITCODE -ne 0) { Write-Warning "find-contacts exited with code $LASTEXITCODE; continuing so the state is still published." }
}

Write-Host "$(Get-Date -Format s) publishing state"
Invoke-Checked 'state-push' { npx tsx src/cli.ts state-push }
npx tsx src/cli.ts export-excel
npx tsx src/cli.ts dashboard
Write-Host "$(Get-Date -Format s) done"
