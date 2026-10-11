# Real job source configuration

`config/sources.json` configures independent job sources. `run` and `discover` load and validate it before querying enabled sources. Company lists are configuration, not orchestration code. Add as many boards as useful for your search.

The shipped configuration includes 16 public company boards across Greenhouse, Lever, Ashby, and Workday, plus a LinkedIn search generated from the existing role/location/age preferences. These company endpoints were queried live on September 15–16, 2026. Availability and openings change. An accessible board does **not** mean it currently contains an eligible job posted within 24 hours.

## Configuration

```json
{
  "request_timeout_ms": 30000,
  "concurrency": 3,
  "sources": [
    { "name": "Gather AI", "type": "greenhouse", "board": "gatherai", "company": "Gather AI" },
    { "name": "TTEC Digital", "type": "lever", "board": "ttecdigital", "company": "TTEC Digital", "region": "global" },
    { "name": "Cognition", "type": "ashby", "board": "cognition", "company": "Cognition" },
    {
      "name": "Centific QA", "type": "workday", "company": "Centific",
      "url": "https://centific.wd1.myworkdayjobs.com/Centific_Global",
      "tenant": "centific", "site": "Centific_Global", "search_text": "QA", "max_pages": 20
    },
    { "name": "LinkedIn", "type": "linkedin", "max_jobs": 500, "max_pages": 5, "max_searches": 18 }
  ],
  "browser_sources": []
}
```

Each entry has a unique `name`, `enabled` (default true), and `max_jobs` (default 5,000). Limits produce an explicit `truncated` flag and `PARTIAL` result. They are not reported as an exhausted source. Public API requests run with the configured concurrency (1–5); browser sources run sequentially using the same persistent session. One failed source does not discard successful independent sources.

`browser_sources` remains accepted for previous configurations. New browser entries should use `sources` with `type: "browser"`. An empty enabled-source list fails clearly. No fixture file is substituted.

`search.json` controls age/role/location filtering after aggregation. Workday's `search_text` is an additional source query; the shipped `QA` query covers roles and descriptions containing QA. Use additional entries with distinct names and other search text, or an empty search string, to broaden that particular board. `enabled` in `sources.json` controls which configured endpoints are queried.

## Source support and timestamps

| Source | Discovery implementation | Publication date | Boundaries |
| --- | --- | --- | --- |
| Greenhouse | Public Job Board API, complete job descriptions | `first_published` | `updated_at` is retained as metadata and never used as publication time. Hosted application URLs preserve board and job IDs. |
| Lever | Public postings API with `skip`/`limit` pagination | `createdAt` when exposed | Creation is a conservative age proxy, potentially earlier than publication. Missing dates remain unknown. EU boards use `region: "eu"`. |
| Ashby | Public Job Postings API, full descriptions and secondary locations | `publishedAt` | Ashby defines this as last publication time; a repost can be fresh. `isListed: false` postings are excluded. |
| Workday | Public careers frontend CXS listing and detail responses | `startDate`; fallback to `postedOn` | Experimental because the frontend contract is not a stable public API. Tenant/site names and endpoints differ. No account creation or login bypass. |
| LinkedIn | Ordinary job-search/detail pages in persistent Playwright | JobPosting JSON-LD or visible time label | Experimental; page layout, login walls, restrictions, and unavailable timestamps may prevent discovery. No private API, proxies, or challenge bypass. |
| Browser | Configured cards, detail selectors, optional JSON-LD and next-page navigation | Configured `datetime`/text or JobPosting JSON-LD | Selectors must match the actual website. Zero matched cards is an error unless `emptyResultsSelector` explicitly indicates no jobs. |

All jobs carry source, available external and ATS IDs, normalized URLs, company, title, location, work mode, full description where the source exposes it, discovery timestamp, and source metadata. HTML is converted to text. Missing descriptions are left empty for scoring to reject rather than replaced by a title-only fake JD.

Unknown dates remain empty and are excluded by default (`allow_unknown_posted_at: false`). Discovery time is never substituted. Relative labels are rounded by websites: `8 hours ago` conservatively means 9 hours for filtering. Date-only values use UTC midnight; exact 8-hour prioritization cannot be guaranteed for dates lacking time/timezone. Workday's `Posted Today` is interpreted as UTC day start. Original labels/date field names remain in metadata where available.

Official contracts: [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html), [Lever Postings API](https://github.com/lever/postings-api), [Ashby Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api).

## LinkedIn and session reuse

Without explicit `urls`, LinkedIn searches are generated from `search.role_keywords` and `search.locations`, falling back to `candidate.preferred_locations`. A Remote search uses the country from the configured candidate location and LinkedIn's Remote filter. Jobs posted within `search.max_age_hours` are requested; returned dates are independently checked before scoring.

`max_searches` limits role/location combinations and reports uncovered combinations. `max_pages` limits pages per search. Existing profile storage in `browser-profile/` is reused; login passwords are not saved by the program. `run --login` / `discover --login` enable legitimate manual login. A CAPTCHA, MFA, restriction, HTTP 403/429, or auth wall stops that source; public company boards continue independently.

## Custom company website

This is a configuration example, not a claim that these selectors work on an arbitrary website:

```json
{
  "name": "Your verified careers page",
  "type": "browser",
  "urls": ["https://careers.example.com/jobs"],
  "company": "Example",
  "cardSelector": "article.job",
  "titleSelector": "h2",
  "linkSelector": "a.job-link",
  "locationSelector": ".location",
  "postedAtSelector": "time",
  "nextPageSelector": "a[rel=next]",
  "emptyResultsSelector": ".no-jobs",
  "max_pages": 10,
  "detail": {
    "descriptionSelector": "main .job-description",
    "postedAtSelector": "time",
    "applicationLinkSelector": "a.apply"
  }
}
```

Inspect the real page and verify selectors with `discover`. `detail: {}` enables JSON-LD hydration without custom detail selectors. Card `companySelector` can replace the static `company`. `externalIdAttribute` captures an ID attribute from each card. Browser discovery reports missing or unusable selectors and retains successful earlier records when a later page is blocked.

## Operational results

Each source records `name`, `type`, `status`, job count, duration, warnings/error, optional URL/screenshot, and truncation. Status is one of `SUCCESS`, `PARTIAL`, `FAILED`, `LOGIN_REQUIRED`, or `BLOCKED_SECURITY`. An HTTP 200 with no jobs is a valid empty board; it does not prove there were eligible applications. A source with missing publication dates reports `PARTIAL` and the number excluded by strict age policy.

Source regression tests use controlled API responses and verify pagination, normalization, publication-date honesty, unlisted jobs, truncation, authentication/restriction stopping, source isolation, and configuration validation. They are explicitly separate from live discovery evidence in run reports.
