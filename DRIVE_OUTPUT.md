# Verified output in Google Drive

The old workflow produced a company/contact report without vacancy links. Its
local verifier wrote files but did not publish them. The new report workflow
connects current job verification to a permanent Drive folder and Google Sheet.

## Output

One folder, **JobAgent Output**, contains:

- **JobAgent - Verified Jobs**: start page, current vacancies ready for review,
  vacancies with fit questions, excluded jobs and unavailable/failed checks.
  Full source JDs appear next to each job link. Experience excerpts, profile
  skill overlaps, gaps, source URLs, hashes and check times are retained.
- **JobAgent - Application Tracker**: initialized empty, for actual submissions
  and responses. It is never replaced by a report refresh. If the user trashes
  it, the deletion is preserved and other outputs still publish.
- **verified.csv**, **evidence.md**, **verification.json**: source text and run evidence.
- **JobAgent Report.xlsx**, the publication receipt and all files explicitly
  listed in `deliverables.json`, uploaded into the same folder with stable IDs.
- **Startup shortlist** appears in the report when `startup-research.json`
  contains reviewed leads. It includes investor sources, named leaders, public
  contact evidence, tailored cold-email drafts and LinkedIn notes of at most
  300 characters. CSV and Markdown versions are saved alongside the report.

The report is generated output. Manual edits to it stop automatic replacement;
keep application progress in the separate tracker. The folder and file IDs are
checkpointed in `drive_output_state.json`, not chosen by newest file name.
Existing external Claude reports are not renamed, moved or overwritten.

## One-time Google connection

Set `drive_output.expected_account` in `config.yaml`, then run:

```powershell
python run.py drive-auth
```

Google requires the account owner to complete consent. The separate
`drive_token.json` requests **drive.file**, allowing the app to create files and
manage files it creates or that are explicitly opened/shared with it. The Gmail
token and its scopes are unchanged. The account is checked before storing the
new token or publishing. Scheduled commands never open consent.

The existing Drive API is sufficient: it imports the generated workbook as a
Google Sheet and updates the same file ID on later runs. Enabling the separate
Sheets API is unnecessary. This uses Google's documented
[file import/update support](https://developers.google.com/workspace/drive/api/guides/manage-uploads)
and [file-scoped authorization](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).

## Refresh and publish

```powershell
# Read the existing DB for leads, fetch current postings, save and publish.
python run.py refresh-report --publish --limit 50

# Optional extra vacancy URLs, using Company and Job Link CSV columns.
python run.py refresh-report --csv research/leads.csv --publish --limit 50

# Optional bounded discovery: configured queries, ten results each; uses Exa credits.
python run.py refresh-report --discover --publish --limit 50

# Publish a prepared bundle without repeating source requests.
python run.py publish-report --out research/report-latest
```

Refresh uses fresh board responses, even for previously cached jobs. It can also
find new QA vacancies on the boards already fetched. Search results remain
unverified until their exact posting is found with a substantive JD. Closed,
blocked, unsupported and missing-description results remain distinguishable.
These commands publish local outreach drafts when a reviewed catalog exists.
They do not create Gmail drafts, send messages, submit applications or call a model.

`refresh-report` prioritizes the catalog's exact job URLs before cached leads.
It does not automatically redo contact or investor research: those facts retain
their original research date. Changed/closed JDs, out-of-scope roles and research
older than 30 days withhold the associated outreach drafts until reviewed.
Third-party email records are labeled and never promoted to verified delivery.
The base resume is a dated output; regenerate it after editing resume data.

`Run-Report.ps1` preserves the command's actual exit status. The optional
`Install-Report-Schedule.ps1` installs a daily 16:00 local-time task with a
30-minute limit and missed-start recovery. It uses no Exa searches by default.
The PC must be available; scheduling does not make this a hosted service.

## What counts as success

`logs/last-refresh-report.json` separates source-check status from publication
status. `research/report-latest/publication.json` is written only after uploaded
content has been read back and compared with the local output. A report with
source failures may still publish its useful partial results while the command
returns exit code 2. Publishing requires evidence checked within 24 hours.

Before replacing the report or its evidence files, the publisher checks that
the current content matches the last verified output or a pending upload. It
preserves unrecognized edits. A lost creation response is reconciled by managed
file identity; an unresolved outcome blocks creation of another copy.

## Limits

- Verification currently supports Ashby, Lever and Greenhouse links. This is a
  bounded search, not an exhaustive inventory of every open job.
- Rules identify relevant skills and explicit gaps; they cannot establish that
  every employer requirement is met. Intermediaries, junior roles, known gaps,
  specialist mobile work and unclear work arrangements need review.
- The old cloud Claude routine is separate and is not controlled by this code.
- LinkedIn and Naukri signed-in pages were observed using Browserbase on
  9 September 2026. New sessions are blocked by exhausted free browser minutes;
  the user asked to keep portal work pending. Saved-context reuse, application
  forms and automatic submission remain unverified. The tracker claims no submissions.
- Reply collection needs separate read-only Gmail consent. This report does not
  label a sent message as delivered or promise responses/interviews.

## Regression checks

`python -m pytest tests/test_output.py tests/test_startup_output.py tests/test_regressions.py -q` covers the
report boundary, repeat publication, interrupted creation, changed content,
stale evidence, formula-like source text, and preservation of application entries.
These offline tests supplement actual live source checks and Drive read-back;
passing them alone is not proof of publication.

The deliverables manifest accepts explicit files directly inside the output
directory. It rejects missing files, traversal, symlinks, duplicate names and
reserved report names before any remote write. Credentials, login context state
and private logs are not part of the manifest.
