# Job Agent fixes and evidence — 9 September 2026

## Startup research and Drive delivery — 9 September 2026

The latest `refresh-report --publish` completed with exit code 0. An independent
Drive inventory found **14 active files in the same existing output folder**:
the permanent Google report and every one of the 13 current local deliverables.
The downloaded Google workbook's cell values match the local XLSX; the uploaded
publication receipt matches byte for byte. Each other uploaded file was also
downloaded and hash-compared by the publisher. Evidence:
`logs/output-validation-2026-09-09.json`,
`research/report-latest/publication.json`, and `logs/last-refresh-report.json`.

- Checked **35 postings**: **34 current full descriptions**, with **7 ready for
  review**, **25 requiring fit review**, **2 excluded**, and **1 closed posting**.
  Rules-based fit does not establish every employer requirement.
- Added a **Startup shortlist** tab with **five roles at four companies**:
  Netomi SDET II and SDET I, Gather AI Senior QA, Certa SDET, and Sprinto Lead SDET.
  Funding/YC sources, public senior-leader profiles, contact provenance, specific
  skill gaps and five unsent outreach drafts accompany the full descriptions.
  LinkedIn notes are **219, 240, 232, 241 and 245 characters**.
- Three named leaders have public third-party email records; these are labeled
  historical/unverified for delivery. Certa's company-published shared founders
  mailbox is distinguished from a personal or hiring inbox. No guessed email
  addresses or claims of hiring-manager ownership were added.
- The reviewed research catalog seeds recurring refreshes. A changed or closed
  JD, out-of-scope role or research older than 30 days withholds the affected
  outreach draft until reviewed. Refreshing a job never silently refreshes the
  contact research date. Cold drafts are local/Drive text, not Gmail drafts.
- Regenerated the base resume from its corrected source so total QA tenure is
  separate from current AI evaluation work, and published DOCX/Markdown copies.
- The old optional Application Tracker was found **trashed**. Its deletion was
  preserved; the publisher now permits other outputs to complete without
  restoring or duplicating it. Manifest files are validated before remote writes.
- **71 pytest regressions passed**, including changed/closed-job draft handling,
  contact age, note length, source requirements, invalid manifests, repeated
  publication, read-back checks and preservation of a trashed optional tracker.

LinkedIn's signed-in feed and Naukri's personal homepage were observed after the
user completed Browserbase login. The sessions subsequently ended; creating a
new session returned HTTP 402, **Free plan browser minutes limit reached**.
The user asked to keep portal work pending. Persisted login reuse, job forms,
automatic application support and submission confirmation remain unverified.
**Zero applications or outreach messages were submitted in this pass.** No model
was switched or called by this reporting workflow. Earlier evidence below is
historical and should not be read as the latest folder inventory or run count.

## Current output delivery — 8 September 2026, 17:57 UTC

The output repair is now verified live. The earlier changes below only produced
local evidence; this pass connected verified vacancies to Google Drive.

- Created one **JobAgent Output** folder containing a permanent **JobAgent -
  Verified Jobs** Google Sheet, a separate **JobAgent - Application Tracker**,
  `verified.csv`, `evidence.md` and `verification.json`.
- First publication and a second refresh used the same folder and file IDs.
  Downloaded the uploaded workbook and evidence files and compared their contents
  with local output. Independent final inventory found exactly five files in the
  folder. Evidence: `research/report-latest/publication.json` and
  `logs/output-validation-2026-09-08.json`.
- Latest scheduled run checked **30 postings**: **29 current listings with full
  descriptions**, comprising **7 ready for review**, **20 with fit questions**
  and **2 excluded**; **1 additional posting was no longer listed**. These are
  observations from public ATS responses at the check time, not guarantees of
  hiring eligibility or future availability.
- Installed **JobAgent Verified Report** at **16:00 local time daily**. Started
  it through Windows Task Scheduler and observed completion with **LastTaskResult
  0**. The next scheduled run was 9 September 2026, 16:00 IST. Evidence:
  `logs/last-refresh-report.json` and `logs/report-refresh.log`.
- Disabled **JobAgent Draft Outreach**, cleared its retired Sheet input and
  disabled the legacy Gmail-outreach switch locally. It no longer schedules
  reads from the superseded cloud contact report. Reviewed CSV draft creation
  remains available when deliberately re-enabled; no email was sent here.
- Fixed incorrect promotion of out-of-scope roles to review, missing flags for
  intermediaries and specialist/junior roles, duplicate ATS URL forms, repeated
  blocked-board requests, and medical-expert evaluation roles entering software
  QA results. Source JDs are preserved, including long descriptions split across
  spreadsheet continuation columns when necessary.
- Publishing uses a separate Google **drive.file** consent/token. The Gmail
  token and the configured Gemini model were not changed. The Drive API imports
  the workbook as Sheets, so the disabled Sheets API does not block publication.
- **56/56 pytest regressions**, **111/111 classification cases**, and **12/12
  offline mailbox cases** passed. Changed Python and PowerShell syntax checks
  passed. Repeat creation, interrupted upload, stale evidence, manual report edits
  and preservation of application-tracker entries have regression coverage.

Remaining limits: the external Claude cloud routine is not controlled by this
repository; it can still create its own separate reports. LinkedIn/Naukri login
and auto-application submission are not completed. Gmail outcome collection needs
separate read consent, and the new tracker has no imported application history.
The legacy complete fetch/LLM/digest/kit pipeline has not been rerun in this pass.
The verified-report workflow is bounded and currently supports three ATS vendors.
The PC must be available for its scheduled refresh. No replies, interviews or
applications are claimed as an outcome of these code fixes.

Implementation: `jobagent/output.py`, `jobagent/drive_output.py`,
`jobagent/outreach/verification.py`, `jobagent/roles.py`, `run.py`,
`Run-Report.ps1`, `Install-Report-Schedule.ps1`, and `tests/test_output.py`.
Setup and operating instructions are in `DRIVE_OUTPUT.md`.
This pass is based on `12e9ec7` on `main`; code changes are local and uncommitted.
No branch switch, commit or push was performed.

## Historical local repair record

The following record describes the earlier pass based on commit
`f95ee2bb00d1492fb5e95bd87ed95119f192cf43`. Its live observations and test counts
apply to that earlier pass and are superseded where the current section differs.

## What is fixed

| Issue | Result and implementation |
|---|---|
| N/A job descriptions and disconnected research | `run.py discover` now invokes Exa for a bounded set of leads. `verify-outreach` fetches current Ashby, Lever or Greenhouse postings and stores full extracted JD, source URL, check time and content hash in a local report. Search snippets and old sheet content never become newly verified JDs. See `jobagent/outreach/verification.py`. |
| Missing versus blocked versus closed | Verification reports `exploratory`, `unsupported_source`, `blocked`, `fetch_error`, `description_missing`, `not_currently_listed`, or `verified_live`. Old JD claims and old statuses are retained separately. Unsupported pages can be extracted with explicit `--firecrawl`, but remain unverified. |
| Guessed contacts and unreviewed copy | Draft input requires an exact email, recent evidence on the stated company website, a current full JD, an in-scope decision and reviewed copy. Public email discovery does not claim deliverability, personal ownership or hiring intent. See `jobagent/outreach/service.py`. |
| Duplicate drafts after a crash or reordered sheet | OS process lock; normalized recipient identity independent of company/contact/row numbering; atomic reservation before Gmail; checkpoint after every result. Legacy ledger recipients still suppress duplicates. An uncertain API outcome is held for human inspection and never automatically retried. |
| Ambiguous Google Sheet | Fixed sheet ID supported and configured locally. Explicit name lookup handles pagination and fails on multiple matching Sheets. Invalid schema fails visibly. Gmail account must match configured `expected_sender`. See `jobagent/outreach/gmail.py` and `run.py`. |
| Unattended consent and misleading draft success | Only `gmail-auth` can open consent. Dry-run with local CSV is offline and creates no draft or ledger. Planned and created counts are separate. Failures/invalid rows produce nonzero exit status. |
| No separation of sent mail and replies | `outreach-status` observes SENT messages and incoming/automatic messages in their threads; it does not equate a draft with a send or a send with delivery. See `jobagent/outreach/outcomes.py`. Local live use needs additional read-only Gmail consent. |
| Search countries treated as home country | `preferences.home_country` is distinct from search markets. The current local profile explicitly says India. Its headline now reflects present AI/LLM QA work. The resume summary now says five years of total software QA rather than implying five years specifically in AI testing. |
| Stale verdict cache | Full posting, salary, classifications, actual prompt/rule code, profile and model/scoring configuration affect hashes. Changing content beyond character 4,000 now invalidates a score. See `models.py`, `matcher.py`. |
| Incomplete model output and incorrect attribution | Every batch index must appear exactly once before any model result is applied. A rules fallback is always labeled rules. |
| Rescore and backlog inconsistency | Classification and match persist in one transaction even if score/category are unchanged. `selection.py` is shared by scoring and the read-only backlog helper; it checks freshness and current policy, including stale LLM rows. |
| Cached jobs consuming a scoring quota | Cache reuse precedes the per-run cap. No-LLM fetches and deferred candidates no longer overwrite unchanged LLM results with rules. |
| Fresh jobs hidden by old high scores | Digest age filtering occurs in the database before the result limit. `digest.only_new` is respected; reporting dates use the local day. Digest labels explicitly say that current listing availability is not reverified. |
| Application kits starve or collide | Existing-kit filtering precedes the quota. Posting URL suffixes distinguish equal company/title names. Kit metadata fingerprints posting inputs, resume content and rendering code; changed inputs trigger regeneration. Legacy folders remain intact. See `resume/tailor.py`. |
| Silent source failures | Feed subquery/detail failures, missing Adzuna credentials, unsupported ATS vendors and Workday JD failures surface in source outcomes while retaining partial results. Pagination uses actual returned page size. |
| False scheduled success | Per-command JSON status records distinguish ok/partial/failed. Daily wrapper uses structured source status, scoring completeness and same-day evidence. The existing draft task action now invokes `Run-DraftOutreach.ps1`, preserving Python's exit code; its schedule was not run. |
| Missing dependencies and regression coverage | `python-docx` declared; optional browser and test requirements separated. New offline regression tests cover the failure modes above. |

## Evidence actually collected

- Read the configured Google Sheet successfully: **65 contact rows, 39 distinct companies, zero Job Link values**. Thus no specific JD can be populated truthfully from that report alone. Local source snapshot: `research/2026-09-08/source-report.csv`; assessed copy: `research/2026-09-08/original-report-checked/`.
- Ran bounded live Exa discovery for five postings, then fetched their public ATS boards. **Four currently listed roles** had full JDs: HighLevel SDET III - AI, YipitData Senior SDET, Jumio SDE II - QA (fixed-term contract), and SavvyMoney Senior SDET India. The HighLevel Payments result was absent from its current board. Evidence: `research/2026-09-08/discovered-verified/`.
- Three of those four need additional review: YipitData asks for seven years of QA, Jumio has contract and office/remote details to clarify, and SavvyMoney asks for six years of automation-framework experience and US Central overlap. A rule match is not proof of hiring eligibility. Four candid application notes are in `research/2026-09-08/application-notes.md`.
- Connected Gmail read-only search against all eight ledger recipients found **eight SENT messages**, dated approximately 06:23–06:24 IST today. The targeted incoming/bounce search returned zero matches. The earlier seven-message count missed the Structured AI message and is superseded. This is a limited search, not proof that no reply exists from another address or that mail was delivered. Evidence: `research/2026-09-08/mailbox-evidence.json`.
- Browserbase REST sessions and CDP navigation worked for both platforms. Follow-up checks at approximately 11:51 IST redirected **both LinkedIn and Naukri to login**. Authenticated access is not established. All sessions created for these checks were released. See `research/2026-09-08/browserbase-check.json`.
- The revised read-only backlog check found **227 eligible fresh candidates** under the new policy. No stored job was rescored during this work.
- Original sheet dry-run: **zero planned or created drafts**; nine rows were already covered by ledger recipients and 56 lacked a valid email. No mailbox mutation was performed.

## Tests and limits

Executed: **33/33 new offline regression tests**, **111/111 existing classification cases**, **12/12 existing mailbox parser cases**. Python syntax and changed PowerShell scripts parsed successfully; Git whitespace checks passed. Tests use temporary databases/ledgers and mocks for remote creation, failures and collisions.

The final command results, timestamp and source fingerprint are saved in `logs/validation-2026-09-08.json`.

Not verified live: a complete fetch/score/digest/kit pipeline; LLM-provider scoring and billing; draft creation using the new implementation; local automatic outcome tracking after additional OAuth consent; authenticated LinkedIn/Naukri browsing or an application submission. Source failures are now observable, but that does not repair an unavailable third-party endpoint.

The existing job database and previous 25 kits were not rebuilt. The external Claude cloud routine is outside this repository and still needs to preserve a stable Sheet ID and produce evidence-compatible rows if it remains in use. Recreating a same-name Sheet does not update the local configured ID automatically. No cloud document was rewritten.

There is still **no auto-apply implementation**. The user must sign in and complete any OTP/CAPTCHA; account navigation and actual submission confirmation must be observed before recording an application. No emails or applications were sent during this work. More application volume cannot be evaluated as successful without verified submissions and response outcomes.

## Current commands

```text
python -m pytest tests/test_regressions.py -q
python run.py classify-test
python run.py mailbox-test --offline
python run.py discover "QA Engineer SDET remote India" --limit 5 --out research/leads.csv
python run.py verify-outreach --csv research/leads.csv --out research/checked
python run.py draft-outreach --csv research/reviewed.csv --dry-run
python tools/browserbase_login_setup.py --site both
```

Verification input needs `Company` and `Job Link`. To verify a public contact, also provide `Email`, `Company Website` and a `Contact Source URL` on that website. Draft input additionally needs `Email Subject`, `Email Body`, `JD Text`, `JD Status=verified_live`, recent `JD Verified At`, `Contact Verification=public_email_found`, recent `Contact Verified At`, `Fit Status=in_scope`, and `Content Status=reviewed`. Verification deliberately sets content to `needs_review`; review claims and gaps before changing that value. Neither an email regex nor a source's self-declared verification status is sufficient.

Optional local reply tracking requires one interactive `python run.py gmail-auth --track-replies` consent, followed by `python run.py outreach-status`. This adds read-only Gmail access to existing draft/Drive scopes. The connected Gmail app already allowed the separate read-only audit above; it does not supply the local Python OAuth token.

Research outputs, profiles, resume data, browser context IDs, OAuth tokens and ledgers are gitignored. The code, tests and this status report remain local uncommitted changes.
