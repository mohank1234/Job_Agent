# Setup that uploads YOUR private data to YOUR private GitHub repository.
# Run it yourself, in a terminal, from anywhere inside the project. Nothing here runs automatically.
# It is safe to run again: finished steps are skipped or simply repeated.
#
#   1. seals the candidate profile + resumes into the encrypted vault and commits it   (skipped if already committed; -ResealVault to redo)
#   2. stores the vault passphrase as the VAULT_PASSPHRASE repository secret
#   3. only with -WithLinkedIn: exports your logged-in LinkedIn session as the LINKEDIN_COOKIES_B64 secret
#      (off by default: using one session from GitHub's servers and your laptop at once may get it signed out)
#   4. publishes the current database as encrypted state                               (skip: -SkipState)
#   5. disables the old local JobLinkDailyRun scheduled task, then starts a workflow run
#
# Requires: gh (logged in), VAULT_PASSPHRASE as a user environment variable, and a prior `npm run dev -- login` for the
# LinkedIn step. The repository must be PRIVATE; the script refuses otherwise.
param([switch]$WithLinkedIn, [switch]$SkipState, [switch]$ResealVault, [string]$Repo = 'mohank1234/job-link')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

# Native commands do not stop a script by themselves; every one of them goes through here.
function Invoke-Checked {
  param([Parameter(Mandatory)][string]$What, [Parameter(Mandatory)][scriptblock]$Command)
  & $Command
  if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE). Nothing after this step ran. Fix the problem shown above and run the script again." }
}

# The state snapshot needs the database to be free.
if (Test-Path 'data/run.lock') {
  $lock = Get-Content 'data/run.lock' -Raw | ConvertFrom-Json
  if (Get-Process -Id $lock.pid -ErrorAction SilentlyContinue) { throw "Another Job Link process (PID $($lock.pid)) is running. Let it finish or close it, then run this script again." }
}

$visibility = gh repo view $Repo --json visibility -q .visibility
if ($LASTEXITCODE -ne 0) { throw "Could not read $Repo. Is gh logged in (gh auth status)?" }
if ($visibility -ne 'PRIVATE') { throw "$Repo is '$visibility'. Personal data may only go to a PRIVATE repository." }

$env:VAULT_PASSPHRASE = [Environment]::GetEnvironmentVariable('VAULT_PASSPHRASE', 'User')
if (-not $env:VAULT_PASSPHRASE) { throw 'The VAULT_PASSPHRASE user environment variable is missing.' }

if ((git ls-files vault/private.enc) -and -not $ResealVault) {
  Write-Host '1/5 Encrypted vault is already committed (use -ResealVault after changing your resumes or profile)'
} else {
  Write-Host '1/5 Sealing and committing the encrypted vault'
  Invoke-Checked 'Sealing the vault' { npx tsx src/cli.ts vault-seal vault/private.enc config/candidate.json resumes }
  Invoke-Checked 'git add' { git add -f vault/private.enc }
  Invoke-Checked 'git commit' { git commit -m 'Update encrypted private inputs' }
  Invoke-Checked 'git push' { git push }
}

Write-Host '2/5 Storing the vault passphrase as a repository secret'
Invoke-Checked 'Storing VAULT_PASSPHRASE' { gh secret set VAULT_PASSPHRASE --repo $Repo --body $env:VAULT_PASSPHRASE }

if ($WithLinkedIn) {
  Write-Host '3/5 Exporting the LinkedIn session (the file is deleted right after upload)'
  Invoke-Checked 'Exporting the LinkedIn session' { npx tsx src/cli.ts export-linkedin-session }
  try { Invoke-Checked 'Storing LINKEDIN_COOKIES_B64' { gh secret set LINKEDIN_COOKIES_B64 --repo $Repo --body (Get-Content data/linkedin-session.b64 -Raw) } }
  finally { Remove-Item data/linkedin-session.b64 -ErrorAction SilentlyContinue }
} else { Write-Host '3/5 LinkedIn session not uploaded (the cloud run does the company boards; LinkedIn runs from your laptop)' }

if (-not $SkipState) {
  Write-Host '4/5 Publishing the current database as encrypted state'
  # Only the very first publish needs a pull (it records which remote state this database descends from). After that, a pull here would
  # replace the local database, including contacts and drafted notes made since, so state-push simply refuses if the cloud is newer.
  if (-not (Test-Path 'data/state-base.txt')) { Invoke-Checked 'state-pull' { npx tsx src/cli.ts state-pull --force } }
  Invoke-Checked 'state-push' { npx tsx src/cli.ts state-push }
} else { Write-Host '4/5 Skipped state (the run starts from the last published state)' }

Write-Host 'Switching off the old local 8am scheduled task so only one runner owns each part of the pipeline'
Disable-ScheduledTask -TaskName 'JobLinkDailyRun' -ErrorAction SilentlyContinue | Out-Null

Write-Host '5/5 Starting a run'
Invoke-Checked 'Starting the workflow' { gh workflow run daily.yml --repo $Repo }
Write-Host "Done. Watch it with:  gh run watch --repo $Repo"
