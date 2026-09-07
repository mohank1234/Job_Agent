"""Public ATS board APIs.

These are the documented, public JSON endpoints that power companies' own
careers pages. No scraping, no auth, no rate-limit games — and postings appear
here the moment they go live, typically hours before any aggregator indexes them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
from dateutil import parser as dateparser

from ..models import Job, clean_html, infer_workplace

UA = {"User-Agent": "job-agent/1.0 (personal job search)"}
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return dateparser.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return None


# --------------------------------------------------------------------------
# Per-vendor adapters: (url_builder, response_parser)
# --------------------------------------------------------------------------

def _greenhouse_url(slug: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def _greenhouse_parse(slug: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        desc = clean_html(j.get("content", ""))
        jobs.append(
            Job(
                source="greenhouse",
                company=slug,
                title=j.get("title", ""),
                url=j.get("absolute_url", ""),
                location=loc,
                workplace=infer_workplace(loc, j.get("title", ""), desc[:600]),
                description=desc,
                posted_at=_parse_date(j.get("updated_at") or j.get("created_at")),
                raw=j,
            )
        )
    return jobs


def _lever_url(slug: str) -> str:
    return f"https://api.lever.co/v0/postings/{slug}?mode=json"


def _lever_parse(slug: str, data: list) -> list[Job]:
    jobs = []
    for j in data or []:
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ""
        desc = clean_html(j.get("descriptionPlain") or j.get("description", ""))
        # Lever's "lists" array carries the responsibilities/requirements
        # sections separately from `description` — dropping it (as this
        # parser used to) discarded the bulk of the posting's substantive
        # content (repo audit 2026-09-07 finding #5: ~4x more content lived
        # here than in `descriptionPlain` on a live board checked that day).
        for section in j.get("lists") or []:
            heading = clean_html(section.get("text", ""))
            body = clean_html(section.get("content", ""))
            if body:
                desc += f"\n\n{heading}\n{body}" if heading else f"\n\n{body}"
        additional = clean_html(j.get("additionalPlain") or j.get("additional", ""))
        if additional:
            desc += f"\n\n{additional}"
        jobs.append(
            Job(
                source="lever",
                company=slug,
                title=j.get("text", ""),
                url=j.get("hostedUrl", ""),
                location=loc,
                workplace=infer_workplace(
                    loc, cats.get("commitment", ""), j.get("workplaceType", ""), desc[:600]
                ),
                description=desc,
                posted_at=_parse_date(
                    (j.get("createdAt") or 0) / 1000 if j.get("createdAt") else None
                ),
                raw=j,
            )
        )
    return jobs


def _ashby_url(slug: str) -> str:
    return (
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        "?includeCompensation=true"
    )


def _ashby_parse(slug: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location", "") or ""
        desc = clean_html(j.get("descriptionPlain") or j.get("descriptionHtml", ""))
        remote = j.get("isRemote")
        workplace = "remote" if remote else infer_workplace(loc, desc[:600])
        jobs.append(
            Job(
                source="ashby",
                company=slug,
                title=j.get("title", ""),
                url=j.get("jobUrl", "") or j.get("applyUrl", ""),
                location=loc,
                workplace=workplace,
                description=desc,
                salary=str(j.get("compensation", {}).get("summaryComponents", "") or ""),
                posted_at=_parse_date(j.get("publishedAt")),
                raw=j,
            )
        )
    return jobs


SMARTRECRUITERS_PAGE = 100
SMARTRECRUITERS_MAX_PAGES = 20   # 2,000 postings is far past any board we track


def _smartrecruiters_url(slug: str, offset: int = 0) -> str:
    return (
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        f"?limit={SMARTRECRUITERS_PAGE}&offset={offset}"
    )


def _smartrecruiters_parse(slug: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("content", []):
        loc_obj = j.get("location") or {}
        loc = ", ".join(
            x for x in [loc_obj.get("city"), loc_obj.get("country")] if x
        )
        remote = loc_obj.get("remote")
        jobs.append(
            Job(
                source="smartrecruiters",
                company=slug,
                title=j.get("name", ""),
                url=(j.get("ref") or "").replace(
                    "api.smartrecruiters.com/v1/companies",
                    "careers.smartrecruiters.com",
                )
                or f"https://careers.smartrecruiters.com/{slug}/{j.get('id','')}",
                location=loc,
                workplace="remote" if remote else infer_workplace(loc),
                description=clean_html(j.get("jobAd", {}).get("sections", {}).get(
                    "jobDescription", {}
                ).get("text", "")),
                posted_at=_parse_date(j.get("releasedDate")),
                raw=j,
            )
        )
    return jobs


def _workable_url(slug: str) -> str:
    return f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"


def _workable_parse(slug: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        loc = ", ".join(
            x for x in [j.get("city"), j.get("country")] if x
        )
        desc = clean_html(j.get("description", ""))
        jobs.append(
            Job(
                source="workable",
                company=slug,
                title=j.get("title", ""),
                url=j.get("url") or j.get("application_url", ""),
                location=loc,
                workplace=infer_workplace(loc, j.get("telecommuting") and "remote" or "", desc[:600]),
                description=desc,
                posted_at=_parse_date(j.get("published_on")),
                raw=j,
            )
        )
    return jobs


ADAPTERS = {
    "greenhouse": (_greenhouse_url, _greenhouse_parse),
    "lever": (_lever_url, _lever_parse),
    "ashby": (_ashby_url, _ashby_parse),
    "workable": (_workable_url, _workable_parse),
}


async def _smartrecruiters_fetch(
    client: httpx.AsyncClient, vendor: str, slug: str
) -> tuple[str, str, list[Job], str | None]:
    """Paginated — a plain single GET (the old `_smartrecruiters_url`/ADAPTERS
    path) silently truncated any board past its first 100 postings (repo
    audit 2026-09-07 finding #7: live-confirmed on Canva and ServiceNow, both
    landing exactly on the 100-record cap)."""
    jobs: list[Job] = []
    try:
        for page in range(SMARTRECRUITERS_MAX_PAGES):
            offset = page * SMARTRECRUITERS_PAGE
            resp = await client.get(
                _smartrecruiters_url(slug, offset), headers=UA, timeout=TIMEOUT
            )
            if resp.status_code == 404:
                return vendor, slug, jobs, "404 — slug not found on this vendor"
            resp.raise_for_status()
            data = resp.json()
            batch = _smartrecruiters_parse(slug, data)
            jobs.extend(batch)
            total = data.get("totalFound") or 0
            if len(batch) < SMARTRECRUITERS_PAGE or len(jobs) >= total:
                break
    except httpx.HTTPStatusError as exc:
        return vendor, slug, jobs, f"HTTP {exc.response.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        return vendor, slug, jobs, f"{type(exc).__name__}: {exc}"
    return vendor, slug, jobs, None


# --- Workday ---------------------------------------------------------------
# Workday is the odd one out: the board is driven by a POST, it caps a page at
# 20 postings, and it needs three identifiers rather than one. So it gets its
# own fetcher instead of an entry in ADAPTERS.
#
# The slug in companies.yaml is "tenant/wdN/site", read straight off the
# careers URL: https://browserstack.wd3.myworkdayjobs.com/External
#                       tenant ^^^^^^^^^^^^ ^^^ wdN      ^^^^^^^^ site
WORKDAY_PAGE = 20
WORKDAY_MAX_PAGES = 25          # 500 postings is far past any board we track


def _workday_parts(slug: str) -> tuple[str, str, str]:
    parts = slug.split("/")
    if len(parts) != 3:
        raise ValueError(f"workday slug must be tenant/wdN/site, got {slug!r}")
    return parts[0], parts[1], parts[2]


def _workday_parse(slug: str, data: dict) -> list[Job]:
    tenant, wd, site = _workday_parts(slug)
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    jobs = []
    for j in data.get("jobPostings", []):
        loc = j.get("locationsText", "") or ""
        path = j.get("externalPath", "") or ""
        # Placeholder until _workday_fetch_detail fills in the real
        # description — bulletFields is typically a handful of short
        # highlights (repo audit 2026-09-07 finding #6: measured as low as
        # 8 characters on a live board, vs. 6,000+ from the detail page).
        desc = clean_html(j.get("bulletFields") and " ".join(j["bulletFields"]) or "")
        jobs.append(
            Job(
                source="workday",
                company=tenant,
                title=j.get("title", ""),
                url=f"{base}/{site}{path}" if path else base,
                location=loc,
                workplace=infer_workplace(loc, "", desc[:600]),
                description=desc,
                posted_at=_parse_date(j.get("postedOn")),
                raw={**j, "_workday_detail_path": path},
            )
        )
    return jobs


async def _workday_fetch_detail(
    client: httpx.AsyncClient, tenant: str, wd: str, site: str, job: Job
) -> None:
    """Fill `job.description` from the per-posting detail endpoint.

    The listing endpoint (`_workday_parse`) only ever returns `bulletFields`
    — a handful of short highlights, not the actual job description. This
    was measured at 8 characters on a live BrowserStack posting during the
    2026-09-07 repo audit (finding #6), against 6,794 characters of real
    HTML description available one request away at this detail endpoint.
    Failures here are non-fatal: the job keeps its bulletFields snippet
    rather than losing the posting entirely.
    """
    path = job.raw.get("_workday_detail_path") if isinstance(job.raw, dict) else None
    if not path:
        return
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
    try:
        resp = await client.get(url, headers={**UA, "Accept": "application/json"},
                                 timeout=TIMEOUT)
        resp.raise_for_status()
        info = resp.json().get("jobPostingInfo") or {}
        full_desc = clean_html(info.get("jobDescription", ""))
        if full_desc:
            job.description = full_desc
            job.workplace = infer_workplace(job.location, "", full_desc[:600])
    except (httpx.HTTPError, ValueError):
        pass   # keep the bulletFields snippet already set by _workday_parse


async def _workday_fetch(
    client: httpx.AsyncClient, vendor: str, slug: str
) -> tuple[str, str, list[Job], str | None]:
    try:
        tenant, wd, site = _workday_parts(slug)
    except ValueError as exc:
        return vendor, slug, [], str(exc)

    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    headers = {**UA, "Content-Type": "application/json", "Accept": "application/json"}
    jobs: list[Job] = []
    try:
        for page in range(WORKDAY_MAX_PAGES):
            body = {
                "appliedFacets": {},
                "limit": WORKDAY_PAGE,
                "offset": page * WORKDAY_PAGE,
                "searchText": "",
            }
            resp = await client.post(url, json=body, headers=headers, timeout=TIMEOUT)
            if resp.status_code == 404:
                return vendor, slug, [], "404 - tenant/site not found"
            resp.raise_for_status()
            data = resp.json()
            batch = _workday_parse(slug, data)
            jobs.extend(batch)
            total = data.get("total") or 0
            if len(batch) < WORKDAY_PAGE or len(jobs) >= total:
                break
    except httpx.HTTPStatusError as exc:
        return vendor, slug, jobs, f"HTTP {exc.response.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        return vendor, slug, jobs, f"{type(exc).__name__}: {exc}"

    # Full-detail pass, bounded concurrency so a large board doesn't fire
    # dozens of simultaneous requests at one tenant.
    detail_sem = asyncio.Semaphore(4)

    async def _fill(job: Job) -> None:
        async with detail_sem:
            await _workday_fetch_detail(client, tenant, wd, site, job)

    await asyncio.gather(*(_fill(j) for j in jobs))
    for j in jobs:
        if isinstance(j.raw, dict):
            j.raw.pop("_workday_detail_path", None)
    return vendor, slug, jobs, None


# Vendors whose board needs more than a single plain GET.
CUSTOM_FETCHERS = {"workday": _workday_fetch, "smartrecruiters": _smartrecruiters_fetch}


async def _fetch_board(
    client: httpx.AsyncClient, vendor: str, slug: str
) -> tuple[str, str, list[Job], str | None]:
    if vendor in CUSTOM_FETCHERS:
        return await CUSTOM_FETCHERS[vendor](client, vendor, slug)
    url_fn, parse_fn = ADAPTERS[vendor]
    try:
        resp = await client.get(url_fn(slug), headers=UA, timeout=TIMEOUT)
        if resp.status_code == 404:
            return vendor, slug, [], "404 — slug not found on this vendor"
        resp.raise_for_status()
        return vendor, slug, parse_fn(slug, resp.json()), None
    except httpx.HTTPStatusError as exc:
        return vendor, slug, [], f"HTTP {exc.response.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        return vendor, slug, [], f"{type(exc).__name__}: {exc}"


async def _gather(boards: dict[str, list[str]], concurrency: int = 12):
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(follow_redirects=True) as client:

        async def guarded(vendor, slug):
            async with sem:
                return await _fetch_board(client, vendor, slug)

        tasks = [
            guarded(vendor, slug)
            for vendor, slugs in boards.items()
            if vendor in ADAPTERS or vendor in CUSTOM_FETCHERS
            for slug in (slugs or [])
        ]
        return await asyncio.gather(*tasks)


def fetch_ats(boards: dict[str, list[str]]) -> tuple[list[Job], list[str]]:
    """Fetch every configured company board concurrently.

    Returns (jobs, errors). A failed board used to be indistinguishable from
    a board with zero open roles today — the error was bound and discarded
    (repo audit 2026-09-07 finding #1). Callers that only want the jobs and
    are fine losing per-board diagnostics can do `jobs, _ = fetch_ats(...)`.
    """
    results = asyncio.run(_gather(boards))
    jobs: list[Job] = []
    errors: list[str] = []
    for vendor, slug, board_jobs, err in results:
        jobs.extend(board_jobs)
        if err:
            errors.append(f"ATS {vendor}/{slug}: {err}")
    return jobs, errors


def verify_boards(boards: dict[str, list[str]]) -> list[tuple[str, str, int, str | None]]:
    """Ping every board and report (vendor, slug, job_count, error).

    Run this after editing companies.yaml — slugs go stale when companies
    switch ATS vendors, and a wrong slug fails silently during a normal fetch.
    """
    results = asyncio.run(_gather(boards))
    return [(v, s, len(j), e) for v, s, j, e in results]
