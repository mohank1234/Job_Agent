# Prepares the PUBLIC copy of the project on your own disk. It does not touch GitHub: no push, no secrets, no repository settings.
#
#   1. audits what would be published (your name, phone, email, links, employers, tokens, vault, database, résumés; values are never printed)
#   2. copies only the publishable files into a new folder next to the project (default ..\job-hub-public) and commits them locally
#   3. prints the exact commands for you to review and run yourself
#
# Run:  ./scripts/prepare-public.ps1
param([string]$Out = (Join-Path (Split-Path $PSScriptRoot -Parent | Split-Path -Parent) 'job-hub-public'))
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$root = (Get-Location).Path

function Invoke-Checked {
  param([Parameter(Mandatory)][string]$What, [Parameter(Mandatory)][scriptblock]$Command)
  & $Command
  if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE). Nothing was published; fix the problem above and run again." }
}

Write-Host '1/3 Auditing what would be published'
Invoke-Checked 'audit-public' { npx tsx src/cli.ts audit-public }
Invoke-Checked 'typecheck' { npx tsc --noEmit -p tsconfig.json }

if (Test-Path $Out) { throw "$Out already exists. Delete it or pass -Out with another folder." }
Write-Host "2/3 Copying the publishable files to $Out"
New-Item -ItemType Directory -Path $Out | Out-Null
$files = git ls-files -co --exclude-standard | Where-Object { $_ -and $_ -ne 'vault/private.enc' }
foreach ($file in $files) {
  $target = Join-Path $Out $file
  New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
  Copy-Item -LiteralPath (Join-Path $root $file) -Destination $target -Force
}
Push-Location $Out
try {
  git init --quiet -b main
  git add -A
  git -c user.name='job-hub' -c user.email='job-hub@users.noreply.github.com' commit --quiet -m 'One project: Job Link (LinkedIn, applications, outreach) and Job Agent (research, drafts, report)'
} finally { Pop-Location }

Write-Host '3/3 Ready. Review the folder, then publish it yourself:'
Write-Host ''
Write-Host "  cd $Out"
Write-Host '  git remote add origin https://github.com/mohank1234/Job_Agent.git'
Write-Host '  git fetch origin'
Write-Host '  git push origin HEAD:main --force-with-lease=main     # replaces the public history with this one clean commit'
Write-Host ''
Write-Host 'Then add the two secrets the merged workflow needs (existing Job Agent secrets stay as they are):'
Write-Host '  gh secret set VAULT_PASSPHRASE --repo mohank1234/Job_Agent        (the value of your VAULT_PASSPHRASE environment variable)'
Write-Host '  gh secret set DATA_REPO_TOKEN  --repo mohank1234/Job_Agent        (fine-grained token: only mohank1234/job-link, Contents read+write)'
Write-Host 'And switch off Actions on the private data repository so only the public one runs:'
Write-Host '  gh api -X PUT repos/mohank1234/job-link/actions/permissions -F enabled=false'
Write-Host 'Finally a check-only run:  gh workflow run daily.yml --repo mohank1234/Job_Agent -f validate_only=true -f link=false'
