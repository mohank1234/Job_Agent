# Setup status

## Scoring model

**Google Gemini is the configured scoring provider.** Account billing has not
been verified by this project. A 429 response establishes a rate-limit failure;
it does not establish whether billing is enabled or whether prior calls cost
money. The measurements below are historical notes and were not rerun in the
8 September 2026 review.

  model:      gemini-flash-latest        1.6 s/job, 6/6 scored, 0 failures
  fallbacks:  gemini-3-flash-preview, gemini-flash-lite-latest
  key:        env var GEMINI_API_KEY  (never stored in this project)

Measured against the alternatives before choosing:

| option                     | speed      | outcome                        |
|----------------------------|------------|--------------------------------|
| gemini-flash-lite-latest   | 0.6 s/job  | works                          |
| gemini-flash-latest        | 1.6 s/job  | works, best discrimination     |
| gemini-3-flash-preview     | 1.7 s/job  | works                          |
| gemma-4-31b-it             | 5.1 s/job  | works                          |
| OmniRoute free pool        | 6-43 s/job | failed ~1 call in 3 - REMOVED  |
| Groq gpt-oss-120b          | -          | 8K tokens/min throttles a batch|
| Cerebras                   | -          | 8,192-token context cap        |

GOTCHA: Gemini spends tokens THINKING before it answers. A small max_tokens
returns finish_reason=length with ZERO output, which looks like a parse bug.
`llm.max_tokens: 30000` is the fix - do not lower it.

To rotate the key: https://aistudio.google.com/apikey, then
`setx GEMINI_API_KEY "new-key"` and sign out/in. Nothing else changes.

GOTCHA: Task Scheduler hands a task a copy of the user environment captured at
logon, so a key added with `setx` afterwards is invisible to the task until you
sign out and back in - the run silently falls back to deterministic scoring.
Run-Daily.ps1 now reads the User scope directly and logs "Recovered
GEMINI_API_KEY from the User environment" when it has to.

GOTCHA: `tools/backlog.py` counts candidates whose `scored_by` is not `llm`.
With no key every pass writes `scored_by='rules'`, the count never falls, and
the scoring loop used to spin for its full 90-minute budget emitting thousands
of identical lines. The loop now stops after two passes with no progress.

OmniRoute has been REMOVED (2026-08-20): the Windows startup shortcut is
deleted, the gateway process is stopped, and nothing launches it at login.
Port 20128 is free. The `omnirate` provider still exists in jobagent/llm.py as
a dormant fallback, but nothing uses it.

The npm package itself is still installed. To reclaim the disk:
    npm uninstall -g omniroute
    Remove-Item -Recurse "$env:LOCALAPPDATA\OmniRoute"
    Remove-Item -Recurse "$env:USERPROFILE\.omniroute"
Backups of the old scripts are in the session scratchpad if you ever want them.

## Sources

| Source | Status |
|---|---|
| 136 company ATS boards | connected |
| RemoteOK, Arbeitnow, Remotive, Jobicy, Himalayas, WeWorkRemotely, WorkingNomads | connected |
| **Job-alert email (Naukri, LinkedIn, Indeed, Instahyre, Hirist, Foundit)** | **BROKEN - see below** |
| Adzuna | not connected - see below |

Measured yield per source (2026-08-24). This is the whole argument for fixing
the mail credentials before adding anything else:

| source          | fetched | matches | hit rate |
|-----------------|---------|---------|----------|
| mail/naukri     |      12 |       9 |  75.00%  |
| lever           |     921 |       7 |   0.76%  |
| remoteok        |     220 |       1 |   0.45%  |
| himalayas       |    5076 |       5 |   0.10%  |
| greenhouse      |    6571 |       2 |   0.03%  |
| ashby           |    3543 |       1 |   0.03%  |
| arbeitnow, jobicy, smartrecruiters, weworkremotely, workingnomads, remotive | 3113 | 0 | 0.00% |

Boards deliberately NOT added, having been probed and found unusable:
Wellfound (no public API - 404/403), Remote.co (no feed, bot-protected),
FlexJobs (paid subscription). Scraping them would breach their terms.

Mail password: env var `JOBAGENT_MAIL_PASSWORD`, not in any project file.
Check everything with: `python run.py check-setup`

BROKEN 2026-08-24: Gmail rejects the app password -
`[AUTHENTICATIONFAILED] Invalid credentials`. The variable is still set (16
chars), so the password itself was revoked - Google does this automatically
when the account password changes or 2-Step Verification is re-set. Nothing in
the agent can fix it. Make a new one at
https://myaccount.google.com/apppasswords, then
`setx JOBAGENT_MAIL_PASSWORD "newpassword"` and reopen the terminal.
This matters more than any other source: the alert mail runs at a ~55% hit
rate against ~0.2% for the ATS boards.

## Singapore

Added 2026-09-01. Two things are in scope there: fully remote roles open to
India (already covered by the normal remote rule), and ONSITE roles in
Singapore where the employer can sponsor an Employment Pass.

### Why the salary matters more than the wording

MOM sets a hard minimum qualifying salary for an EP. Below it, no employer can
sponsor you however willing they are, so salary decides eligibility as a matter
of law. 2026 floors for the youngest applicants, rising with age:

    most sectors        S$5,600 / month     (S$6,000 from 1 Jan 2027)
    financial services  S$6,200 / month     (S$6,600 from 1 Jan 2027)

Applicants must also clear COMPASS (40 points), which scores the employer's
workforce mix as well as the candidate - so a role can clear the salary bar and
still fail. Treat "meets the floor" as necessary, not sufficient.

Re-check these every January; they have risen every year. Set them in
`profile.yaml -> preferences.singapore`, and keep
`config.yaml -> sources.mycareersfuture.ep_min_monthly_sgd` in step.

### The source

MyCareersFuture (`api.mycareersfuture.gov.sg`) is Singapore's official
government job board, run by Workforce Singapore. Public JSON API, no key. It
is the only Singapore source that reports salary, so each posting it returns is
annotated with a `[work authorisation: ...]` line saying whether the pay clears
the floor. `singapore_eligibility()` in roles.py reads that line.

Verdicts: `sponsors` (+7 and uncapped) / `unspecified` (+1, uncapped - worth one
email) / `below_floor` and `locals_only` (both capped at 40, effectively out).

### The trap: "QA Engineer" does not mean software there

In India a QA Engineer tests software. In Singapore the same title is mostly
factory, marine, chemical or semiconductor quality control. The first live run
returned 127 postings of which only a minority were software.

`RE_INDUSTRIAL_QUALITY` in roles.py matches the BODY, not the title, and only
fires when the posting shows no software-testing evidence at all - so a genuine
software role at a manufacturer is unaffected.

Expect most Singapore QA postings to come from staffing agencies; that market
is heavily agency-mediated. The existing -14 penalty handles them.

## Workday boards

Added 2026-08-25 for BrowserStack. Workday is the odd vendor out: the board is
driven by a POST, a page caps at 20 postings, and it needs three identifiers
instead of one, so it has its own fetcher in `ats.py` rather than an entry in
`ADAPTERS`.

The slug is `tenant/wdN/site`, read straight off the careers URL:

    https://browserstack.wd3.myworkdayjobs.com/External
            \________/ \_/               \______/
              tenant   wdN                 site

To find whether a company uses Workday, fetch its careers page and look for
`myworkdayjobs.com`. Many large employers do, so this unlocks more than one
company.

## Company lists

`companies.yaml` holds boards to FETCH from - only worth an entry when the
vendor actually serves postings for that slug, so every one was probed first.
`profile.yaml -> preferences.preferred_companies` is a +12 scoring bonus that
applies to a company from ANY source, so a name pays off there even with no
ATS board at all. Add a name to both when it has a board; to preferred only
when it does not.

ALWAYS run `python run.py verify` after editing companies.yaml. On 2026-08-25
it found 23 greenhouse slugs that had been added without probing and returned
404 on every run - including two typos (`truveda` for truveta, `otypy` for
otipy) and two that were already live under a different vendor (`kredx` and
`whatfix` on smartrecruiters). Four exact duplicates went with them. That is 27
wasted requests per run, failing silently. Boards: 111 live.

Note `verify` distinguishes a 404 (delete the slug) from a ReadTimeout (keep
it - groww and mindtickle both time out intermittently but are live).

Every board slug in companies.yaml is also in preferred_companies. Keeping a
board for a company IS the statement that we want to work there, so the +12
should follow it. Before 2026-08-26 it did not: 64 of 110 tracked boards -
OpenAI, Anthropic, Cognition, Veeva, MongoDB, Notion among them - scored as
unknown employers, because preferred held full names ("veeva systems") while a
board reports its slug ("veeva"). Add new board slugs to both files.

`_company_delta` checks in the order explicit-agency, preferred, fuzzy
pattern. A firm named in staffing_companies must never be rescued by a
preferred name that happens to be a substring of it, and a preferred company
must not be penalised because the generic pattern fires on one word in its
name.

## Startup & VC-ecosystem discovery strategy

HIGH PRIORITY, set 2026-09-06. Traditional job portals (LinkedIn, Naukri,
Indeed) get exactly one route in — the mailbox reader on saved-search alerts
(see Sources above). They are not the primary discovery channel; startup
ecosystems are. `companies.yaml` already leans this way by accident (OpenAI,
Cursor, Cognition, Pinecone, Langfuse, Braintrust, Decagon, Sierra... are all
YC/VC-backed AI companies) — the goal now is to make that deliberate.

Two discovery approaches, run in parallel, not sequentially:

- **Job First** — an advertised posting -> JD vs. resume -> research the
  company -> find the founder/CTO/QA-head/recruiter -> prepare outreach.
- **Company First** — a strong startup found via YC, Wellfound, or a VC
  portfolio -> understand its product -> judge whether the QA + Automation +
  AI/LLM/RAG/agent-testing background is genuinely relevant -> check its
  careers page/ATS board -> find a decision-maker even with no open role.

Ecosystems to mine, in rough priority order: Y Combinator (company directory
+ Work at a Startup), Wellfound, and the portfolio pages of Sequoia, Accel,
Lightspeed, Peak XV, a16z, Techstars, Antler, 500 Global, General Catalyst,
Bessemer.

**Mechanics, given the public-API-only rule at the top of `companies.yaml`:**
a VC/accelerator's own portfolio or company-directory PAGE is public
marketing content, not a protected job board — fetching it to read off
company NAMES is fine. What happens next is the existing companies.yaml
workflow unchanged: guess/confirm the ATS vendor+slug, probe it against that
vendor's public JSON API, run `python run.py verify`, keep only what's live,
add every new slug to BOTH `companies.yaml` and
`profile.yaml -> preferences.preferred_companies` (see Company lists above).
Wellfound and "Work at a Startup" themselves are treated like LinkedIn/Naukri
— no public API, no scraping — so they're a place to read postings by eye,
not an automated fetch source.

**Verifying an ATS hit is really the target company, not a slug collision.**
A guessed vendor+slug returning HTTP 200 with a non-empty jobs array is not
proof of identity — ATS slugs are global per-vendor namespaces, not scoped to
any list of candidates. Short or generic company names (Pulse, Aviary,
Castle, Tailor, Extend, Momentic, Profound) have repeatedly turned out to
belong to an unrelated company that happens to hold the same slug (a Qatar
healthcare-staffing firm under Greenhouse `pulse`; a biotech company under
Greenhouse `profound` with roles like "Senior Scientist, Cardiovascular and
Metabolic Disease"). Collision rate on short/dictionary-word names has run
around 1-in-10 across two discovery passes (YC pass, then the 10-VC-firm
pass). Before trusting any ATS hit: sample 1-2 actual job titles/locations
from the response and check they're thematically and geographically
consistent with the target company. Build this into the probing step itself,
not as an afterthought — it's cheap and it's the only thing standing between
the pipeline and silently onboarding the wrong company.

**Fetching VC/accelerator portfolio pages: a fallback ladder, not a single
retry.** These pages fail in three distinct ways that need different
handling — don't collapse them into one "retry with WebSearch" step:
1. *Wrong URL* (fixable) — the obvious path 404s; a WebSearch for
   `site:<domain> portfolio` or similar usually finds the real one (e.g.
   Peak XV's real path is `/our-companies`, not `/companies`).
2. *Stale or misleading content at a plausible URL* (fixable by trying a
   sibling page) — e.g. a16z's `/portfolio/` reads stale, but
   `/investment-list/` is current.
3. *Genuinely absent server-rendered content* (not fixable by more
   fetching) — company names exist only as image logos with no text in the
   server-rendered HTML (e.g. 500 Global). No amount of WebFetch retrying
   recovers names that were never in the payload, and falling back to
   WebSearch here tends to surface only already-huge, already-tracked
   companies rather than fresh mid-size ones — treat this as a flagged
   low-value source, not silent "coverage."

**Non-traditional titles.** Don't gate discovery on the literal string "QA
Engineer" — also watch for AI Quality, AI/LLM/Model/Agent Evaluation, RAG
Evaluation, AI Reliability, AI Data Quality, Quality Engineering, SDET, Test
Automation Engineer. `matching.qa_roles_only` in config.yaml still requires
a testing/eval word in the TITLE (see Scoring model above) — this widens
which words satisfy that check, it does not turn the check off.

**Direct/exploratory outreach.** A startup with a strong product and no
advertised opening is still worth reaching if the fit looks real. These are
not jobs — `jobs.db` stores fetched postings only — so they're tracked
separately in `outreach_targets.yaml`: company, product, why it fits, the
decision-maker, and a public contact channel. Always label these "No
confirmed opening — Direct/Exploratory Outreach" in any digest or resume
kit; never present one as an active job.

**Prioritization.** Score on profile fit + real product + real hiring
activity + funding/stability signal + a reachable decision-maker. Belonging
to YC/Sequoia/a16z/etc. is a discovery filter that surfaces candidates, not
a qualifying signal that promotes one on its own.

## Enrichment and outreach adapters (2026-09-08)

`jobagent/enrich/` (Exa, Firecrawl) and `jobagent/outreach/gmail.py` were
added to support the strategy above with real tools instead of hand-research
every time. All three are live-verified — `python run.py check-setup`
checks each with a real minimal request, not a config-presence check.

**Exa** (`jobagent/enrich/exa.py`) — neural search for companies, jobs, and
professional profiles. Get a key at [exa.ai](https://exa.ai) (dashboard ->
API key). Free tier checked 2026-09-08: $20 signup credit + $10/month
recurring, no card. `setx EXA_API_KEY "..."`.

**Firecrawl** (`jobagent/enrich/firecrawl.py`) — turns a URL into clean
markdown, including JS-rendered pages a plain HTTP GET can't read (verified
live against `ycombinator.com/companies/...` pages, which are blocked
entirely in some sandboxed environments but reachable from here). Get a key
at [firecrawl.dev](https://firecrawl.dev). Free tier checked 2026-09-08:
1,000 credits/month, no card. `setx FIRECRAWL_API_KEY "..."`.

Both raise a typed `AdapterError` (`jobagent/enrich/_errors.py`) with a
`.kind` — `unauthorized` / `forbidden` / `rate_limited` / `server_error` /
`timeout` / `connection_error` / `invalid_response` — instead of collapsing
every failure into "no results," so a bad key never looks like an empty
search.

**Gmail** (`jobagent/outreach/gmail.py`) — draft creation only. There is no
send function: sending needs a real policy (recipient confidence,
suppression list, dedup ledger) that doesn't exist yet, so the adapter
stops at "create a reviewable draft."

1. Google Cloud Console -> enable the Gmail API -> OAuth 2.0 Client ID
   (Desktop app) -> download JSON, save as `credentials.json` in the
   project root (gitignored).
2. Run `python run.py gmail-auth` yourself, in your own terminal — it opens
   a real browser and needs you to click Allow. This can't be automated and
   shouldn't be: it's you granting access to your own Gmail account.
3. `token.json` (also gitignored) is written after that and refreshes
   itself; delete it to force re-authorization.

**Browserbase:** `jobagent/enrich/browserbase.py` and
`python tools/browserbase_login_setup.py --site both` provide bounded sessions
with persistent contexts and human sign-in. The optional controller dependency
is in `requirements-browser.txt`; a hosted browser does not require a local
Chromium download. Recording, browser logs and automated CAPTCHA solving are
disabled. The script confirms account navigation separately from reaching the
login page and never submits applications. Sessions consume Browserbase quota.

Hunter, Apollo and Antigravity have no callable integration in this project.
Provider pricing and account quotas require checking with the provider; key
presence does not prove a free tier or a working connection.

The current verification, draft safeguards, read-only reply evidence and test
instructions are documented in [AUDIT_STATUS.md](AUDIT_STATUS.md).

## Runs by itself

| Task | Time | Does |
|---|---|---|
| JobAgent Daily | 10:30 | check key, fetch every source, score, write the digest, build application kits |
| JobAgent Watch | on logon + every network connect | runs the day as soon as it becomes possible, if it has not run yet |
| JobAgent Retry | 11:00 | backstop: runs the day if nothing else has covered it |
| JobAgent Catchup | 20:00 | clear any scoring backlog |

The morning moved from 08:00 to 10:30 on 2026-09-01; Watch and Retry were added
the same day. All of them are the same script, and the whole design turns on one
question: **has today already succeeded?** `Run-Daily.ps1` writes
`logs\last-success.txt` only once the digest is actually on disk, so a run that
fetched but crashed before the digest leaves no marker and the day counts as
uncovered. Passes started with `-OnlyIfFailed` read that marker and exit in about
two seconds when the day is already covered.

Watch is the one that makes a missed run recover quickly. `StartWhenAvailable`
already re-runs Daily after the machine was *off*, but it does nothing for the
more common case of the machine being awake with no internet. So Watch triggers
on logon (3-minute delay) and on NetworkProfile event 10000, "network
connected", which covers wifi reconnects, cable plug-in and VPN coming up. It
probes real TCP 443 connectivity before committing to a run, because the event
arrives while the link is still settling and also fires for links that route
nowhere. No connection means it exits without touching the marker, so the next
event picks the day up. `-NotBefore 10:30` stops a morning logon pulling the
whole schedule forward.

Two things about Watch are load-bearing and easy to break:

- Its logon trigger **must** pass `-User`. Without it that is an any-user logon
  trigger, which only an administrator may register, and the entire task is
  refused with `Access is denied`.
- Event 10000 fires in **pairs**, and Daily, Watch and Retry can all run the
  pipeline. `MultipleInstances IgnoreNew` only guards a task against *itself*,
  never against a sibling, so it cannot prevent two concurrent pipelines. That
  is what `logs\run.lock` is for: an exclusively-held file handle, which needs
  no privileges, works across sessions, and is released by Windows if the
  process is killed. A pass that cannot take the lock logs and exits 0.

That marker exists because a task's exit code used to be worthless here:
`Run-Daily.ps1` sets `$ErrorActionPreference = "Continue"` and never exited
non-zero, so a crashed pipeline still reported success. It now checks
`$LASTEXITCODE` after fetch and digest and exits 1 if either failed, which is
also what Task Scheduler shows as Last Run Result. `apply-kit` failing is
logged as a warning but does not fail the day - the digest already tells you
where to apply, and re-fetching everything to rebuild a kit is a poor trade.

`Install-Schedule.ps1` sets `$ErrorActionPreference = "Stop"` for the same
class of reason. `Register-ScheduledTask` reports a refusal as a
NON-terminating error, so the installer used to print `Installed: JobAgent
Watch` for a task that had just been denied and did not exist.

Application kits are built by the daily run as of 2026-08-26. Before that only
the digest was automated, so kits existed only for days someone ran
`apply-kit` by hand - four digests, one applications folder. A job that already
has a kit from an earlier day is skipped rather than re-tailored; pass `--all`
to force a rebuild. Tailoring is pure regex over the job description with no
API call, so the step is fast and cannot hang the run.

GOTCHA: the tasks shipped with `DisallowStartIfOnBatteries` set, which is the
Windows default. On a laptop that is not plugged in, the morning run does not
fail - it sits in state `Queued` forever and the log simply never gains a new
line. `StopIfGoingOnBatteries` would also kill a run mid-flight on unplug. Both
are now off on all three tasks, and `Install-Schedule.ps1` sets them explicitly
- it did not, so until 2026-09-01 every re-run of the installer quietly put the
bug back. To confirm:

    (Get-ScheduledTask -TaskName "JobAgent Daily").Settings.DisallowStartIfOnBatteries

A full run now takes seconds, not hours. Logs: `D:\job-agent\logs\`.
Run one now: `Start-ScheduledTask -TaskName "JobAgent Daily"`

---

# What is still left to provide

Everything below is free and needs no credit card. The agent already works
without any of it — these two items unlock the coverage it currently cannot
reach: **Indian onsite jobs (Hyderabad, Bengaluru)** and **Naukri / LinkedIn**.

---

## 1. Adzuna API key — unlocks Indian job listings

**Why:** Adzuna is a licensed job aggregator that indexes Indian listings,
including many that originate on Naukri. It is the fastest route to Hyderabad
and Bengaluru onsite roles, which the current sources barely carry.

**Time:** ~2 minutes.

### Steps

1. Go to **https://developer.adzuna.com/**
2. Click **Sign up** (top right). Email + password. No card.
3. Confirm the email they send you.
4. Log in and open **https://developer.adzuna.com/admin/access_details**
5. Copy the two values shown there:
   - **Application ID** — short, looks like `a1b2c3d4`
   - **Application Key** — long, looks like `9f8e7d6c5b4a3210fedcba9876543210`

### Where to put them

Open `D:\job-agent\config.yaml`, find the `adzuna:` block at the bottom, and
set three fields:

```yaml
  adzuna:
    enabled: true                       # was false
    countries: [in, us, gb, ae, sg, ca, au]
    app_id: "PASTE_APPLICATION_ID"
    app_key: "PASTE_APPLICATION_KEY"
```

### Check it worked

```
python run.py fetch --no-llm
```

The "Aggregator feeds" count should jump by several hundred, and
`python run.py stats` should start showing an `adzuna` source row.

---

## 2. Gmail app password — unlocks Naukri, LinkedIn, Indeed, Instahyre

**Why:** these portals have no public API and their terms prohibit automated
access. But they will email you saved-search alerts. Reading your own inbox
breaks nobody's terms and carries no ban risk. This is the only safe route to
their listings.

**Time:** ~10 minutes, most of it creating the job alerts.

### Step A — create the job alerts (do this first)

On each portal, run the search you want and click "Create job alert" /
"Save this search", set frequency to **Daily**:

| Portal | Where |
|---|---|
| Naukri | https://www.naukri.com — search, then "Create Job Alert" |
| LinkedIn | https://www.linkedin.com/jobs — search, toggle "Set alert" to on |
| Indeed | https://in.indeed.com — search, then "Get new jobs by email" |
| Instahyre | https://www.instahyre.com — preferences drive their emails |
| Hirist | https://www.hirist.tech — "Create job alert" |
| Foundit | https://www.foundit.in — "Job alert" |

Searches worth creating, given your profile:

- `QA Engineer Hyderabad`
- `SDET Hyderabad`
- `QA Automation Engineer Hyderabad`
- `QA Engineer Bangalore`
- `Software Test Engineer remote India`
- `AI QA Engineer` / `LLM Evaluation Engineer`

Alerts land in your inbox from tomorrow. Nothing to parse until then.

### Step B — create an app password

**Do not use your real Gmail password.** An app password is a separate
16-character password that only works for mail, and you can revoke it any time
without touching your account.

1. Go to **https://myaccount.google.com/security**
2. Turn on **2-Step Verification** if it is not already on (required before
   app passwords appear).
3. Go to **https://myaccount.google.com/apppasswords**
4. Name it `job-agent` and click **Create**.
5. Copy the 16-character password it shows, e.g. `abcd efgh ijkl mnop`
   (spaces do not matter).

### Step C — store it as an environment variable

Open PowerShell and run, with your own value:

```powershell
setx JOBAGENT_MAIL_PASSWORD "abcdefghijklmnop"
```

Then **close and reopen the terminal** — `setx` only affects new shells.

The password is never written into any file in this project.

### Step D — point the agent at your mailbox

In `D:\job-agent\config.yaml`, find `sources: mailbox:` and set:

```yaml
  mailbox:
    enabled: true                              # was false
    host: imap.gmail.com
    port: 993
    user: "your-address@gmail.com"      # your address
    password_env: JOBAGENT_MAIL_PASSWORD
    folder: INBOX
    max_age_days: 7
    max_messages: 400
```

### Check it worked

```
python run.py mailbox-test
```

This connects **read-only** — it cannot mark anything read, move it, or delete
it — and prints exactly which postings it found in your alert emails, grouped
by portal. Nothing is written to the database until you are happy with what it
shows.

---

## Optional: filter the alerts into their own folder

If your inbox is busy, make a Gmail filter that labels alert mail as
`JobAlerts`, then set `folder: JobAlerts` in the config. The agent then reads
only that label. Faster, and it never touches the rest of your mail.

---

## Summary of what to hand over

| Item | Where from | Goes into |
|---|---|---|
| Adzuna Application ID | developer.adzuna.com/admin/access_details | `config.yaml` -> `adzuna.app_id` |
| Adzuna Application Key | same page | `config.yaml` -> `adzuna.app_key` |
| Gmail app password | myaccount.google.com/apppasswords | env var `JOBAGENT_MAIL_PASSWORD` |
| Your email address | you | `config.yaml` -> `mailbox.user` |
| Daily job alerts | Naukri / LinkedIn / Indeed / Instahyre / Hirist / Foundit | your inbox |

No other keys, accounts or payments are needed beyond the free `GEMINI_API_KEY`
above. The LLM scoring runs on Gemini's free tier — OmniRoute (mentioned
earlier in this doc) was removed on 2026-08-20 and nothing launches it; this
line used to still credit it, which was itself the kind of doc/code
divergence flagged in the 2026-09-07 repo audit (finding #15) — fixed here.


## Google Drive output

See [DRIVE_OUTPUT.md](DRIVE_OUTPUT.md) for the verified vacancy report, one-time
Drive consent, permanent folder and file IDs, daily refresh, and read-back checks.
The legacy cloud contact report is a separate workflow and is not the new output.
