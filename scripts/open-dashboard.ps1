# Opens the dashboard with the newest cloud state. Read-only for GitHub: it only downloads the encrypted state and the readable
# reports branch from your PRIVATE data repository, then builds reports/dashboard.html locally.
param([string]$Data = 'mohank1234/job-link')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:VAULT_PASSPHRASE = [Environment]::GetEnvironmentVariable('VAULT_PASSPHRASE', 'User')
if (-not $env:VAULT_PASSPHRASE) { throw 'The VAULT_PASSPHRASE user environment variable is missing.' }
if (Test-Path 'data/run.lock') { throw 'A Job Link process is running; open the dashboard when it has finished.' }
# Your own local work is published first (refused if the cloud is newer), then the newest cloud state replaces the local copy (a backup is kept).
npx tsx src/cli.ts state-push
npx tsx src/cli.ts state-pull --force
npx tsx src/cli.ts export-excel
npx tsx src/cli.ts dashboard --open
