# Job Agent - unattended daily run.
#
# 1. Confirms the Gemini API key is present (no local gateway any more).
# 2. Fetches from every source, stores everything, classifies.
# 3. Works through the LLM scoring backlog in repeated passes.
#
# Gemini scores at ~1.6 s/job, so a full backlog clears in minutes rather than
# hours. Scoring still runs in bounded passes with a wall-clock budget so a
# rate-limit pause can never hang the task. Every pass checkpoints after each
# batch of four, so stopping early never loses work.
#
# Run it by hand:      powershell -ExecutionPolicy Bypass -File D:\job-agent\Run-Daily.ps1
# Or install it:       see Install-Schedule.ps1

param(
    [int]$ScoreMinutes = 90,      # total wall-clock budget for scoring
    [switch]$SkipFetch,
    [switch]$OnlyIfFailed,        # catch-up pass: no-op unless today has not succeeded
    [string]$NotBefore = "10:30"  # a catch-up never runs earlier than the daily slot
)

$ErrorActionPreference = "Continue"

# Python writes UTF-8; without this PowerShell decodes its output as ANSI and
# every bullet or dash lands in the log as mojibake ("A 0 ?? B 0").
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$today  = Get-Date -Format 'yyyy-MM-dd'
$log    = Join-Path $root "logs\daily-$today.log"
$marker = Join-Path $root "logs\last-success.txt"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

function Write-Log {
    param([string]$m)
    $line = "{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Host $line
    # PowerShell 5.1's Tee-Object/Out-File default to UTF-16 here, which makes
    # the log unreadable in every normal tool. Force UTF-8.
    Add-Content -Path $log -Value $line -Encoding UTF8
}

function Write-LogStream {
    param([Parameter(ValueFromPipeline = $true)]$line)
    process {
        if ($null -ne $line) {
            Write-Host $line
            Add-Content -Path $log -Value ([string]$line) -Encoding UTF8
        }
    }
}

Write-Log "=== Job Agent daily run ==="

function Test-DayDone {
    # Run-Daily writes the marker only once the digest is on disk, so its
    # content is the cheapest honest proof that today is already covered.
    $statePath = Join-Path $root "logs\daily-status-$today.json"
    if (-not (Test-Path -LiteralPath $statePath)) { return $false }
    try {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        return ($state.status -eq 'ok') -and (Test-Path $marker) -and ((Get-Content $marker -Raw).Trim() -eq $today)
    } catch { return $false }
}

function Test-Online {
    # A catch-up pass fires on network-connect events, which arrive while the
    # link is still settling and also for links that route nowhere (a VPN
    # coming up, a captive hotel wifi). ICMP is blocked often enough to be
    # useless, so this opens a real TCP 443 connection to hosts the run needs.
    foreach ($h in @("www.google.com", "api.github.com")) {
        $c = New-Object System.Net.Sockets.TcpClient
        try {
            $wait = $c.BeginConnect($h, 443, $null, $null)
            if ($wait.AsyncWaitHandle.WaitOne(3000) -and $c.Connected) { return $true }
        } catch {
        } finally {
            $c.Close()
        }
    }
    return $false
}

# --- 0. Catch-up guard ------------------------------------------------------
# Three tasks can reach this script: Daily at 10:30, Retry at 11:00, and Watch,
# which fires on logon and on every network-connect event so a missed run
# starts the moment the machine or the link comes back rather than waiting for
# 11:00. Retry and Watch pass -OnlyIfFailed, which makes them no-ops unless the
# day is genuinely uncovered.
if ($OnlyIfFailed) {
    # Never earlier than the daily slot. Without this, a logon at 07:00 would
    # quietly pull the whole schedule forward.
    $threshold = [datetime]::ParseExact("$today $NotBefore", "yyyy-MM-dd HH:mm", $null)
    if ((Get-Date) -lt $threshold) {
        Write-Log "Catch-up pass: before the $NotBefore slot. Nothing to do yet."
        exit 0
    }
    if (Test-DayDone) {
        Write-Log "Catch-up pass: today already succeeded. Nothing to do."
        exit 0
    }
}

# --- 0b. Single-instance lock ----------------------------------------------
# Daily, Retry and Watch can all run this pipeline, and Watch can fire several
# times in a minute while a link flaps. MultipleInstances only guards a task
# against itself, never against a sibling task, so that setting cannot prevent
# two concurrent pipelines. An exclusively-held file is the cheapest lock that
# works across tasks and sessions, needs no privileges, and that Windows
# releases automatically if the process is killed.
$lockPath = Join-Path $root "logs\run.lock"
try {
    $lock = [System.IO.File]::Open($lockPath, "OpenOrCreate", "ReadWrite", "None")
} catch {
    Write-Log "Another pass already holds logs\run.lock. Leaving it alone."
    exit 0
}

if ($OnlyIfFailed) {
    # Re-check now the lock is held: a run may have finished in the window
    # between the check above and this line.
    if (Test-DayDone) {
        Write-Log "Catch-up pass: a run finished while this one was starting. Nothing to do."
        exit 0
    }
    if (-not (Test-Online)) {
        # Deliberately not a failure. Leaving the marker alone means the next
        # network-connect event picks the day up, which is the whole point of
        # the Watch task; burning a full failed fetch here would achieve
        # nothing except a misleading log.
        Write-Log "Catch-up pass: no usable internet connection. Waiting for the next event."
        exit 0
    }
    Write-Log "Catch-up pass: no successful run recorded for $today - running the day now."
}

# Anything appended here fails the run, which is what the 11:00 retry reads.
$failures = @()

# --- 1. Scoring model -------------------------------------------------------
# Scoring runs on the Gemini free tier now. There is no local gateway to start,
# so this only confirms the key is present. Without it the run still completes:
# every job gets its deterministic score, just no LLM verdict.
# Task Scheduler gives the task a snapshot of the user environment taken at
# logon, so a key added with setx afterwards is missing here even though it is
# set. Read the User scope directly to sidestep that.
if (-not $env:GEMINI_API_KEY) {
    $fromUser = [Environment]::GetEnvironmentVariable("GEMINI_API_KEY", "User")
    if ($fromUser) {
        $env:GEMINI_API_KEY = $fromUser
        Write-Log "Recovered GEMINI_API_KEY from the User environment."
    }
}

if (-not $env:GEMINI_API_KEY) {
    Write-Log "ERROR: GEMINI_API_KEY is not set for this account."
    Write-Log '       Fix: setx GEMINI_API_KEY "your-key"  (key from'
    Write-Log "       https://aistudio.google.com/apikey), then sign out and in."
    Write-Log "       Continuing with deterministic scoring only."
} else {
    Write-Log "Gemini key present. The scoring command verifies its configured provider; billing is not measured."
}

$env:PYTHONIOENCODING = "utf-8"

# --- 2. Fetch ---------------------------------------------------------------
# `fetch` almost never exits nonzero even when a source fails outright — a
# bad ATS slug or a rejected mailbox login used to vanish into a discarded
# error with the exit code staying 0 (repo audit 2026-09-07 finding #1 and
# #14: "success" was reported with zero verification that sources actually
# came back). Since that fix, per-source failures surface as "note:" lines
# in fetch's own output; scan for them here so a degraded run is at least
# visibly flagged, without turning source flakiness into a full pipeline
# failure — a digest built from partial coverage is still useful, and
# `Preserve successful partial results and record failures separately` per
# policy, not `failed request -> no jobs available`.
$sourceCoverageIssues = @()
if (-not $SkipFetch) {
    Write-Log "Fetching all sources..."
    $fetchOutput = python run.py fetch --limit 120 2>&1
    $fetchOutput | Write-LogStream
    if ($LASTEXITCODE -ne 0) {
        $failures += "fetch exited $LASTEXITCODE"
        Write-Log "ERROR: fetch failed (exit $LASTEXITCODE)."
    }
    $statusPath = Join-Path $root "logs\last-fetch.json"
    try {
        $fetchStatus = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
        $sourceCoverageIssues = @($fetchStatus.sources | Where-Object { $_.status -in @('error', 'partial') })
        if ($fetchStatus.status -ne 'ok' -or ([datetime]$fetchStatus.finished_at).ToLocalTime().ToString('yyyy-MM-dd') -ne $today) {
            $failures += 'fetch status is incomplete or stale'
        }
    } catch {
        $failures += 'fetch status missing or invalid'
    }
    if ($sourceCoverageIssues.Count -gt 0) {
        Write-Log "WARNING: $($sourceCoverageIssues.Count) source(s) failed this run - digest coverage is partial. See notes above."
    }
    Write-Log "Fetch complete"
}

# --- 3. Work the scoring backlog -------------------------------------------
# The backlog only counts jobs still lacking an LLM verdict. If the LLM is
# unavailable every pass falls back to rules, the count never moves, and
# without this guard the loop spins for the full wall-clock budget writing
# thousands of identical log lines. Two passes with no progress and we stop.
$deadline = (Get-Date).AddMinutes($ScoreMinutes)
$pass = 0
$previous = [int]::MaxValue
$stalled = 0
while ((Get-Date) -lt $deadline) {
    $remaining = [int](python tools\backlog.py | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0) {
        $failures += 'backlog check failed'
        break
    }
    if ($remaining -le 0) {
        Write-Log "Scoring backlog is empty."
        break
    }
    if ($remaining -ge $previous) {
        $stalled++
        if ($stalled -ge 2) {
            $failures += "scoring stalled with $remaining eligible candidates left"
            Write-Log "Scoring made no progress in $stalled passes - $remaining left unscored."
            Write-Log "       These keep their deterministic score. Cause is usually a"
            Write-Log "       missing or rate-limited API key; see the note above."
            break
        }
    } else {
        $stalled = 0
    }
    $previous = $remaining
    $pass++
    Write-Log "Scoring pass $pass - $remaining candidates still unscored"
    python run.py score --limit 200 2>&1 | Select-String -Pattern "LLM-scored|note:" |
        Write-LogStream
    if ($LASTEXITCODE -ne 0) {
        $failures += "scoring incomplete (exit $LASTEXITCODE)"
        break
    }
}

# --- 4. Digest --------------------------------------------------------------
if ((Get-Date) -ge $deadline -and $remaining -gt 0) {
    $failures += 'scoring time budget ended before backlog completion'
}
# The digest is the deliverable, so this is the step that decides whether the
# day succeeded. `digest` can also exit 0 having written nothing, so the file
# itself is checked rather than the exit code alone.
$digest = Join-Path $root "digest_$today.md"
python run.py digest 2>&1 | Write-LogStream
if ($LASTEXITCODE -ne 0) {
    $failures += "digest exited $LASTEXITCODE"
    Write-Log "ERROR: digest failed (exit $LASTEXITCODE)."
} elseif (-not (Test-Path $digest)) {
    $failures += "digest wrote no file"
    Write-Log "ERROR: digest reported success but digest_$today.md is missing."
}

# --- 5. Application kits ----------------------------------------------------
# The digest says which jobs are worth applying to; this builds the tailored
# resume and note for each one so there is nothing left to prepare by hand.
# Only jobs without a kit from an earlier day are built, so a match that has
# been in the list for a week is not re-tailored every morning.
#
# --top is a LIFETIME CEILING, not a daily quota, and it is the single number
# that decides how many kits ever exist. Each run takes the N highest-scoring
# A/B/C jobs and drops any that already have a kit folder, so once N kits exist
# the same N jobs come back every day and nothing is built until a new posting
# displaces one of them. At --top 8 that produced NOTHING on nine of eleven
# runs between 2026-08-26 and 08-31: ten kits in twelve days.
#
# 25 tracks the size of the digest rather than a sliver of it. Tailoring is
# pure regex over the job description with no API call, so a larger number
# costs seconds of CPU and some disk, not quota, and cannot hang the way a
# scoring pass can.
$kitCount = 25
Write-Log "Building application kits..."
python run.py apply-kit --top $kitCount 2>&1 | Write-LogStream
if ($LASTEXITCODE -ne 0) {
    # Not a failure for the day: the digest already tells you where to apply,
    # and re-running the whole fetch to rebuild a kit is a poor trade.
    Write-Log "WARNING: apply-kit exited $LASTEXITCODE - build the kits by hand with"
    $failures += "application kits incomplete (exit $LASTEXITCODE)"
    Write-Log "         python run.py apply-kit --top $kitCount"
}

# --- 6. Outcome -------------------------------------------------------------
# The 11:00 retry reads this marker, so only write it for a run that fetched
# cleanly and left a digest on disk. Exit code matters too: it is what Task
# Scheduler records as Last Run Result, and until now a crashed pipeline still
# reported 0 there.
if ($SkipFetch) {
    try {
        $fetchStatus = Get-Content -LiteralPath (Join-Path $root 'logs\last-fetch.json') -Raw | ConvertFrom-Json
        if ($fetchStatus.status -ne 'ok' -or ([datetime]$fetchStatus.finished_at).ToLocalTime().ToString('yyyy-MM-dd') -ne $today) {
            $failures += 'no complete fetch verified today'
        }
    } catch { $failures += 'no fetch status available' }
}
if ($failures.Count -eq 0 -and $sourceCoverageIssues.Count -eq 0) {
    @{status='ok'; finished_at=(Get-Date).ToString('o'); failures=@()} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $root "logs\daily-status-$today.json") -Encoding UTF8
    Set-Content -Path $marker -Value $today -Encoding UTF8
    if ($sourceCoverageIssues.Count -gt 0) {
        Write-Log "=== Done WITH PARTIAL COVERAGE: digest_$today.md ($($sourceCoverageIssues.Count) source(s) failed - see WARNING above) ==="
    } else {
        Write-Log "=== Done. Digest: digest_$today.md ==="
    }
    if ($lock) { $lock.Close() }
    exit 0
}

Write-Log "=== FAILED: $($failures -join '; ') ==="
@{status='partial'; finished_at=(Get-Date).ToString('o'); failures=$failures; source_issues=$sourceCoverageIssues} | ConvertTo-Json -Depth 6 |
    Set-Content -LiteralPath (Join-Path $root "logs\daily-status-$today.json") -Encoding UTF8
if ($lock) { $lock.Close() }
Write-Log "    No success marker written, so the day stays uncovered: the next"
Write-Log "    logon or network-connect event retries it, and 11:00 is the backstop."
exit 1
