# Job Agent fixes and evidence — 8 September 2026

Reviewed and changed locally on `main`, based on commit `f95ee2bb00d1492fb5e95bd87ed95119f192cf43`. No branch switch, commit or push was performed. The earlier Claude audit covered an older commit; its reported successes are historical claims, not verification of this working tree.

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
