# Job Link

A persistent TypeScript job-search workflow with SQLite state, Playwright sessions, truthful fit scoring, and explicit human safety gates.

## Setup

```powershell
npm install
npm run init
```

Fill `config/candidate.json` and place truthful resumes in `resumes/`. Run `npm run dev -- resumes` to register PDF resumes. The system will not infer missing candidate facts from filenames or unread resume content.

## Commands

```powershell
npm run init
npm run dev -- report
npm run dev -- resumes
npm run dev -- discover
npm run import -- jobs.e2e.json
npm run dev -- run
npm run dev -- review 2
npm run dev -- outreach
npm run dev -- find-contacts
npm run dev -- digest
npm run dev -- linkedin-status
npm run dev -- state-pull --force
npm run dev -- state-push
npm run dev -- applied 123
npm run build
```

The browser profile is stored in `browser-profile/`. It is never populated with passwords by the application. CAPTCHA, MFA, identity checks, restrictions, unknown mandatory questions, and LinkedIn sends require human handling. LinkedIn automation is deliberately defaulted to `false` in configuration.

`run` processes eligible imported jobs into a durable application queue. `review <jobId>` opens the real application URL in headed Playwright, records safety stops and missing required fields, and captures a screenshot. It does not submit an application automatically.

Configure browser discovery sources in `config/sources.json`. Each source defines URLs and semantic selectors for its job cards. The framework provides extension contracts for job sources under `src/sources/` and ATS adapters under `src/ats/`; site-specific adapters can be added without changing the database or orchestrator.

`run` now discovers every configured source before processing jobs, and the default batch limit is 100 (`config/application.json`). An empty `browser_sources` array means the run can only process previously imported jobs; it cannot see jobs from external sites until at least one source is configured.

### Finding people to contact

`npm run dev -- find-contacts` is read-only: for each company where a real live job was found it looks up the company's LinkedIn page, takes LinkedIn's internal company number from its employees link, and searches **only people who work there today** (a plain keyword search returns anyone who merely mentions the company) for hiring managers, QA/engineering managers, recruiters/talent acquisition, HR, CEO/founder/CTO. It writes them into `config/contacts.json` plus a CSV at `reports/contacts-export.csv`, and drafts a connection note for each strong one. `--company Infor "Some Co"` searches named companies instead. Verified on real LinkedIn: ktree.com, Infor and Krishu Global returned 18 people (talent-acquisition managers, recruiters, HR managers, a CEO), 10 kept, 6 notes drafted.

- A company name too short to match safely (e.g. `ICE`) is not guessed; add its LinkedIn slug to `config/linkedin-companies.json`, e.g. `{ "ICE": "intercontinental-exchange" }`.
- **Who is written to:** at most `max_contacts_per_company` (3) people per company in total, however many jobs it has. HR/recruiting people and managers come first (one of each if available, then the next best), and one CEO/founder-level contact only if a place is left.
- **The note is tailored** to the reader (HR: asks for guidance on the hiring process; managers: a short chat about what the team needs; leadership: brief and about quality at the company) and to the job (AI-testing roles lead with your AI-testing skills, SDET/automation roles with automation skills, manual roles with manual/functional skills). Every fact comes from `config/candidate.json`.
- The company number is remembered in the database, so later runs skip the two lookup pages.
- It never clicks anything but a search; it stops on a login wall, CAPTCHA or restriction like every other LinkedIn step.
- `npm run dev -- draft-outreach` re-writes the drafts that have not been sent (no LinkedIn access). Notes use only facts from `config/candidate.json` (years of experience, automation skills) and stay within 300 characters.

### LinkedIn connection outreach

`run` drafts connection-request notes (never sends them) for jobs scoring at or above `auto_apply_min_score`, but it can only draft to contacts it knows about. Add real people yourself in `config/contacts.json` (or run `find-contacts` above to populate it for you):

```json
{ "contacts": [ { "company": "Acme", "name": "Asha Kapoor", "title": "QA Hiring Manager", "profileUrl": "https://www.linkedin.com/in/example", "relevanceScore": 90 } ] }
```

Then run `npm run dev -- outreach`. For each drafted contact it opens LinkedIn's own invitation dialog for that person (no profile visit), checks the dialog is for the right person, clicks "Add a note", types the note, then **shows you the exact note** and asks:

```
Send this invitation to Naveen P (Manager, Talent Acquisition at Infor) at Infor?
  "Hi Naveen, I'm a QA engineer with 5 years of experience in Selenium and Playwright automation. ..."
[y] send   [n] not now   [s] never for this person   [a] send this and all the rest   [q] stop
```

Only your keypress sends it (the tool then clicks "Send invitation" and checks the dialog closed). `a` approves everything still queued after you have read the first one. `--dry-run` fills and shows every note and sends nothing; `--headless` hides the browser. When LinkedIn shows no invitation dialog (already connected, invitation pending, Connect not offered) it opens the profile page to tell which, and reports it instead of guessing. If LinkedIn says the weekly invitation limit is reached, the batch stops. Nothing is ever sent without your keypress; there is no unattended mode. Every outcome (sent, pending, already connected, never) is also written to `data/outreach-ledger.jsonl`, outside the database, and re-applied after any `state-pull`, so restoring an older database can never make the tool invite someone twice. If the browser closes or crashes mid-batch it is reopened and the batch carries on, and a send that failed is tried again on the next run. (Verified on real LinkedIn: 6 real invitation dialogs opened and filled with the right note, nothing sent.)

### Priority order, exact notes, Excel and the dashboard

- **Work order.** Jobs are worked in age batches (0-1h, 1-3h, 3-6h, 6-12h, 12-24h, older, unknown). Inside a batch: Hyderabad, then Remote, then Bangalore, then everywhere else (`location_priority` in `config/search.json`), then fit. LinkedIn searches run location by location in that order, and `locations` now includes `India` so other-city jobs are found and ranked last instead of dropped.
- **Connection notes are exactly 300 characters** (message + one blank line + your name without a trailing initial, or `signature` in `config/outreach.json`). Only skills the posting asks for and your profile has are named (`jdRelevantSkills`). `exact_note_length: false` returns to the older flexible note. Sending still needs your keypress per note.
- `npm run dev -- export-excel` (also run after `outreach`) writes `reports/job-outreach.xlsx`: **Completed jobs** (every person written to is sent / pending / connected), **Outreach in progress**, and **People contacted** with each note.
- `npm run dev -- dashboard [--open]` writes `reports/dashboard.html`: KPIs, the age x location grid, pipeline status, what needs you, latest outreach, runs, source/errors, GitHub run history for Job Link and the Job Agent repo (`config/integrations.json`), and a snapshot of the Job Agent database.
- **Fewer emails.** The daily run keeps ONE issue labelled `job-link-status`, rewritten silently each run; GitHub emails you only the first time, or when the set of jobs waiting on you changes.

### One project, public code, private data

Job Link (this folder) and Job Agent (`agent/`, Python) are one project with one workflow (`.github/workflows/daily.yml`) and one daily email (Job Agent's results email, with the Job Link summary appended). The code lives in a PUBLIC repository so Actions minutes are free; nothing personal does:

- the profile, résumés, database, readable reports and the LinkedIn session live only in the PRIVATE data repository (`vault/private.enc` is encrypted; `state` and `reports` branches), reached with the `DATA_REPO_TOKEN` secret;
- Job Link's console output goes to a file and is only pushed to the private `reports` branch; the public run log shows exit codes only; no artifacts, step summaries or issues are published;
- `npm run dev -- audit-public` (also run by CI) fails if a file to be published contains your name, phone, email, links, employers or any token;
- `./scripts/prepare-public.ps1` audits and builds the public copy next to the project and tells you the commands to publish it; `./scripts/open-dashboard.ps1` downloads the newest private state and opens the dashboard.
- Jobs the poster named under "Meet the hiring team" on LinkedIn are written to first (job poster, then the next hiring-team member), before people found by company search.

### Easy Apply

LinkedIn jobs whose page has an Easy Apply form are queued like any other application and filled by `src/ats/linkedin-easy-apply.ts`: contact details LinkedIn prefilled are accepted when they agree with `config/candidate.json`; the résumé chosen for the job is selected if LinkedIn already lists it (by name) or uploaded if not, and the page is checked to show that résumé as selected; the default "Follow company" tick is removed and the paid "top choice" option is never touched; each page is walked with **Next/Review** until the final page. Any required question only you can answer stops the walk on that page and becomes a question (`questions`, `answer`; saved answers are reused). Common questions are answered from `config/candidate.json` whatever their wording: notice period (converted to days or months when the question asks), current/expected CTC (to LPA or INR when asked), total years of experience, LinkedIn URL and preferred location; "How many years of experience do you have with X?" is answered from `skill_years`, e.g. `"skill_years": { "Selenium": 5, "API Testing": 4 }`. Yes/no questions and skills not listed stay questions for you. It stops before **Submit application**. `npm run dev -- review <id>` re-fills and stops; `npm run dev -- review <id> --submit` clicks Submit once for that one job, only after every check passes, and records LinkedIn's "application was sent" notice as the confirmation. Already-applied postings are recognised and skipped. Jobs whose LinkedIn page links to the employer's own site go to the employer-form adapters instead.

**When a question needs you** (a visible-browser `run` or `review` in a terminal): the queue pauses on that tab and does nothing else: no refresh, no navigation, no next job. Answer in the browser, then type `continue` in the terminal; the tool prints each answer it saves (check them: LinkedIn pre-fills answers from earlier applications), remembers the ones that are plain facts (years, notice period, CTC, location; never legal or consent answers) and carries on from the same page. Type `skip` to leave the job for later: the tool closes the tab itself and the browser stays open for the next job. Unattended runs (`--headless`, the daily cloud run) record the question instead. Dropdowns and typeaheads are only chosen when your answer names exactly one option (the same wording repeated counts as one); otherwise it is a question. A city typeahead can offer the same city in two states, so give `location` in full, e.g. `"Hyderabad, Telangana, India"`. A saved LinkedIn application that reopens on a later page is walked from page 1 again, and a ready application names any optional question left blank ("optional questions left blank: …") so you see it before `--submit`.

**What is never applied to:** internships, trainee, fresher, apprentice and junior roles (`exclude_internships` in `config/search.json`), and postings written for less experience than `min_job_experience_years` (4): a range counts by its top ("2-3 years" is skipped, "3-5 years" is kept), an open minimum by itself ("3+ years" is skipped, "4+" kept), and only overall experience counts ("2 years with Cypress" does not). Postings that state no experience are kept. The same role at the same company is applied to once: a LinkedIn repost or a copy on a second career site (same company, title and description within 30 days) becomes `DUPLICATE`. Applications already queued that break these rules are retired as `EXCLUDED` at the next run, with their open questions and unsent outreach notes; anything submitted is never touched. Closed postings (`CLOSED_JOB`) stay skipped on rediscovery; technical problems (`RETRYABLE_TECHNICAL_FAILURE`, `VALIDATION_FAILURE`, `UNSUPPORTED_ATS`) are listed separately in the digest and never become questions for you. Give `phone_country_code` (e.g. `"+91"`) in `config/candidate.json`: phone boxes with their own country picker then get the number with its dialling code, since such a picker can default to another country and show your number as a foreign one. `review <id>` works on a job of any age (the freshness window only decides which new jobs a run picks up). Set `JOB_LINK_RECORD_VIDEO=1` to save a video of each application tab to `reports/videos/`, and `JOB_LINK_DEBUG_EASY_APPLY=1` to print what each Easy Apply page contained.

## Current architecture

- `src/database.ts`: SQLite schema and durable state
- `src/config.ts`: validated configuration
- `src/fit.ts`: profile-grounded fit scoring
- `src/jobs.ts`: duplicate-safe job persistence
- `src/browser/session.ts`: headed persistent Playwright context
- `src/sources/`: configurable and pluggable job discovery sources
- `src/ats/`: generic and site-specific application adapters
- `src/pipeline.ts`: resumable fit-to-application queue
- `src/outreach-framework.ts`: contact ranking and duplicate-safe message drafts
- `src/outreach.ts`: bounded message generation and validation
- `src/linkedin-people-search.ts`: read-only LinkedIn people search per company; never connects or messages
- `src/contacts-export.ts`: merges found people into `config/contacts.json` and writes `reports/contacts-export.csv`
- `src/linkedin-connect.ts`: opens a contact's real profile, verifies identity, fills the invitation note; never clicks Send
- `src/outreach-send.ts`: durable orchestration over drafted outreach, pausing for your explicit Send confirmation
- `src/report.ts`: run summary counts
- `src/linkedin-budget.ts`: shared daily LinkedIn page budget and the pause after a login/security block
- `src/vault.ts`, `src/state-sync.ts`: encrypted private inputs and history-free encrypted database state
- `src/digest.ts`: the morning summary (what to submit, what to apply to manually, what is unhealthy)

## Automation: GitHub Actions every morning, your laptop as a fallback for LinkedIn

`.github/workflows/daily.yml` runs at **08:00 IST** on GitHub's servers (plus a backup slot at 10:00 IST that does nothing if the first already ran; GitHub sometimes delays or drops a scheduled run), with the laptop off: company boards → scoring → queue → fills employer application forms with the chosen résumé → hiring-contact search and drafted notes → saves the encrypted database → writes a **digest** (run summary, plus an issue that mentions you when something needs you).

**Nothing is ever submitted or sent automatically.** Those clicks stay yours.

### LinkedIn from the cloud

LinkedIn runs in the cloud job only when the `LINKEDIN_COOKIES_B64` secret exists (`setup-github.ps1` uploads your exported session; `-SkipLinkedIn` leaves it out). What a real test showed: with that session, LinkedIn served GitHub's server about a dozen pages (searches and job pages) in 73 seconds, then answered **HTTP 429 (too many requests)** when three search pages were requested 3 seconds apart. That is a rate limit on pace, so every LinkedIn page load is now spaced by at least `linkedin_min_seconds_between_pages` (default 10 s). Whether GitHub's addresses stay accepted at that pace is confirmed by running it, and the safety net makes a "no" harmless:

- Any 429, login wall, CAPTCHA or restriction **pauses LinkedIn for `linkedin_block_cooldown_hours` (default 72) in that environment**. Nothing retries or works around it. The company boards keep running.
- `scripts/local-linkedin.ps1` does the LinkedIn half from your own connection. Install it as a daily 09:00 task with `./scripts/setup-local-linkedin.ps1` (`-Remove` to uninstall). The pause is per environment, so a cloud pause never stops your laptop, while the daily page budget is shared.
- `config/safety.json`: `linkedin_daily_page_limit` (**0 = no daily cap**, which is how this project is set; a number caps every feature and environment per UTC day), `linkedin_min_seconds_between_pages` (10 s: the pace LinkedIn tolerates, not a quota), `linkedin_block_cooldown_hours`. `npm run dev -- linkedin-status` shows today's use. Detail pages are only opened for titles that match your QA/testing search, and a posting found by several searches is fetched once (matched by LinkedIn's job number, not its tracking URL).
- **Job search coverage:** LinkedIn's results page holds 25 jobs but draws only the first 7 until the list is scrolled. The source scrolls each page until all 25 are drawn, follows further pages while they are full and still bring new jobs, and reads each job's age from LinkedIn's own text ("10 minutes ago", "Reposted 1 week ago") instead of a date-only value. One real search went from ~1 job to 50 (2 pages). Jobs scoring `review_min_score` (60) or more are queued; a required skill missing from `config/candidate.json` (for example CI/CD) still rules a job out, so add skills you really have.
- No stealth, fingerprint spoofing or evasion is used anywhere.

### Setup

The repository must be **private** (the scripts refuse otherwise).

```powershell
./scripts/setup-github.ps1               # vault, passphrase secret, encrypted state, cloud run  (-WithLinkedIn also uploads your LinkedIn session; off by default)
./scripts/setup-local-linkedin.ps1       # optional: the laptop fallback task
```

The vault (`vault/private.enc`, AES-256-GCM) holds your candidate profile and résumés. Its passphrase exists only in your user environment variable `VAULT_PASSPHRASE` and as a GitHub secret, never in the project folder. Until `setup-github.ps1` has run, the scheduled workflow reports what is missing instead of failing.

### Each morning

The digest (Actions → latest run → summary, or the emailed issue) is a **to-do list**: open items stay on it until you close them.

1. **Ready to submit** — forms are filled, résumé attached. `npm run dev -- state-pull --force`, then `npm run dev -- review <id> --submit` opens the form for your final check, then `npm run dev -- state-push`. (`state-push` refuses to overwrite anything newer than what you pulled.)
2. **Apply manually** — LinkedIn Easy Apply, or an employer page whose form needs an Apply click first. The right résumé is already chosen. When you have applied: `npm run dev -- applied <id>` (then `state-push`).
3. **Needs your input** — a form asked something only you can answer (`npm run dev -- questions`, `answer`).
4. **Connection requests** — `npm run dev -- outreach` shows each drafted note; press `y` (or `a` for all) to send it.

After a fix here to how a form is read or filled, `npm run dev -- recheck` reopens every "needs your input" or failed application that is still fresh and never got submitted, so the next `run` reprocesses it with the current code instead of showing stale results. Nothing revisits those on its own otherwise.
