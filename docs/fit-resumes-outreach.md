# Fit scoring, resumes and outreach

## Truthful fit scoring

`config/candidate.json` is the only source of verified candidate capabilities. PDF extraction does **not** add skills or experience to the candidate profile. Scoring records matched, missing, mandatory missing and preferred missing requirements, the decision and a readable reason in the job record.

The current model allocates up to 25 points for a QA/testing title, 50 for verified skill requirements, 15 for experience and 10 for the preferred location. Explicit mandatory skills receive greater weight; preferred requirements receive lower weight. Mandatory gaps cap the score below the default review threshold and produce NOT_ELIGIBLE. Sparse descriptions also cannot receive a high score. Java, JavaScript, Python, Selenium, Cypress and Playwright remain distinct skills. Explicit recognized alternatives such as "Java or Python" accept the verified alternative without claiming the other skill.

Total experience does not prove years spent using a particular technology. Country-specific authorization requires `candidate.authorized_countries`, for example `["India"]` **only if factually correct**. The existing bare `work_authorization: "Yes"` does not establish authorization in every country. Missing facts must be verified by the candidate before adding them.

This is deterministic requirement parsing, not a comprehensive natural-language reasoner. Unrecognized tools, qualification wording and complex nested alternatives may not be classified; review the persisted explanations and actual JD. All mandatory application questions remain independently validated before submission. PDF facts omitted from candidate.json remain unverified even if the resume contains them.

## Resume registry

Normal `run` scans every PDF in `resumes/`; `npm run dev -- resumes` provides a manual rescan. The registry records database ID, filename, file size, SHA-256 content hash, category, extracted skill labels and extraction errors. Removed files become unavailable while historical application references remain intact. PDF text is extracted locally with `pdf-parse`, without modifying or uploading the file during registration.

Filename classification recognizes MAIN and senior/Sr; other PDFs become GENERAL_QA. Selection compares actual extracted content with the JD and considers AI relevance and seniority. The returned selection includes the resume ID and reason. A missing category falls back to the best available file. An extraction failure is recorded and lowers that file's selection rank; scanned/image-only PDFs require a text PDF or reviewed metadata. Supported registration/upload format is PDF.

Optional `config/resumes.json`:

```json
{
  "overrides": [
    {
      "filename": "existing-resume.pdf",
      "category": "MAIN",
      "keywords": ["LLM testing", "Promptfoo"]
    }
  ]
}
```

Overrides affect resume classification/selection only. Add only keywords genuinely represented by that document. They do not change the authoritative candidate profile or the resume content.

### Actual local document validation

All three existing PDFs were read successfully; extraction errors: **0**. GENERAL_QA and MAIN are byte-identical (550,414 bytes); SENIOR_QA is a different document (550,880 bytes). Controlled JD selection against these actual local files produced:

| Controlled JD | Selected registry ID/category |
| --- | --- |
| Senior AI QA Engineer: LLM, RAG, Promptfoo, agents | 2 / MAIN |
| Senior QA Engineer: Java, Selenium, TestNG, API automation | 3 / SENIOR_QA |
| General QA Engineer: manual, regression, API, Selenium | 1 / GENERAL_QA |

These are **controlled selection checks using real local documents**, not evidence of a live application or upload. The MAIN/GENERAL PDFs include skills absent from candidate.json, and describe Playwright as basics. The system has not promoted those resume statements into new candidate facts.

## Contact discovery and outreach

`config/contacts.json` configures disclosed contacts and read-only public contact feeds:

```json
{
  "contacts": [],
  "sources": [
    {
      "name": "Company public team page",
      "company": "The exact company name",
      "url": "https://example.org/team",
      "type": "structured-data",
      "enabled": false
    }
  ]
}
```

The disabled URL above is an example only. An enabled `structured-data` source must expose JSON-LD Person records with `name`, `jobTitle` and a profile `url` or LinkedIn `sameAs`. A `json` source returns an array in the same shape as manual `contacts` records:

```json
{
  "company": "The exact company name",
  "name": "A real verified person",
  "title": "Their actual role",
  "profileUrl": "https://example.org/people/actual-profile",
  "relevanceScore": 0
}
```

Company matching excludes common legal suffixes. Ranking recognizes recruiting, QA leadership, engineering management and founders; no names or employment relationships are invented. The included live contact configuration is empty. No contacts can be claimed discovered until a real configured source returns evidence. Universal LinkedIn people search and LinkedIn messaging UI automation are **not implemented**.

For high-fit jobs, orchestration discovers matching configured contacts and persists concise connection drafts as OUTREACH_READY. Drafts say "interested" unless a durable SUBMITTED application exists. Notes adapt to the configured limit (at most 300 Unicode characters), and their exact count is validated and stored. No automatic sends occur. Existing contact history suppresses another draft to the same person, even for another job; company cooldown prevents repeated outreach for separate jobs during the configured interval. Multiple relevant contacts for the same job are capped by `max_contacts_per_job`.

HTTP authentication, restriction and rate-limit failures become explicit source errors. No CAPTCHA, login or platform restriction is bypassed. Configured public feeds and structured data are supported; arbitrary team-page layouts, inferred contact identities and authenticated LinkedIn discovery are not supported.

## Verification

`npx tsx --test tests/fit-resumes-outreach.test.ts` passes **14 controlled regression tests**, including exact skill boundaries, preferred versus mandatory clauses, country authorization, sparse descriptions, real PDF parser integration on generated test documents, rescans/removal, selection by actual content, note length, structured contact parsing, durable deduplication and cooldown. `npm run dev -- resumes` and `npm run build` also passed during this component validation.
