"""Public aggregator APIs — broad coverage to complement the targeted ATS boards.

All of these are official, documented, no-auth JSON endpoints (Adzuna needs a
free key). None of them require scraping.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from dateutil import parser as dateparser

from ..models import Job, clean_html, infer_workplace

UA = {"User-Agent": "job-agent/1.0 (personal job search)"}
TIMEOUT = httpx.Timeout(25.0, connect=10.0)


def _date(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return dateparser.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return None


def _get(client: httpx.Client, url: str, **kw):
    resp = client.get(url, headers=UA, timeout=TIMEOUT, **kw)
    resp.raise_for_status()
    return resp.json()


def _remoteok(client) -> list[Job]:
    data = _get(client, "https://remoteok.com/api")
    jobs = []
    for j in data[1:] if isinstance(data, list) else []:  # [0] is a legal notice
        desc = clean_html(j.get("description", ""))
        jobs.append(
            Job(
                source="remoteok",
                company=j.get("company", ""),
                title=j.get("position", ""),
                url=j.get("url", ""),
                location=j.get("location", "") or "Remote",
                workplace="remote",
                description=desc,
                salary=" ".join(
                    str(x) for x in [j.get("salary_min"), j.get("salary_max")] if x
                ),
                posted_at=_date(j.get("epoch") or j.get("date")),
                raw=j,
            )
        )
    return jobs


def _remotive_rows(data) -> list[Job]:
    return [
        Job(
            source="remotive",
            company=j.get("company_name", ""),
            title=j.get("title", ""),
            url=j.get("url", ""),
            location=j.get("candidate_required_location", "") or "Remote",
            workplace="remote",
            description=clean_html(j.get("description", "")),
            salary=j.get("salary", "") or "",
            posted_at=_date(j.get("publication_date")),
            raw=j,
        )
        for j in data.get("jobs", [])
    ]


def _remotive(client, cfg=None) -> list[Job]:
    """Generic feed plus role-targeted queries.

    The generic feed is capped and dominated by non-QA roles, so QA postings
    fall off the end. Querying for the target roles surfaces them. This widens
    the funnel — everything fetched is still stored and classified.
    """
    jobs = _remotive_rows(_get(client, "https://remotive.com/api/remote-jobs?limit=200"))
    for term in (cfg or {}).get("search_terms") or []:
        try:
            jobs += _remotive_rows(_get(
                client,
                f"https://remotive.com/api/remote-jobs?limit=100&search={quote(term)}",
            ))
        except (httpx.HTTPError, ValueError):
            continue          # one bad query must not lose the whole source
    return jobs


def _arbeitnow(client) -> list[Job]:
    data = _get(client, "https://www.arbeitnow.com/api/job-board-api")
    jobs = []
    for j in data.get("data", []):
        remote = j.get("remote")
        loc = j.get("location", "") or ""
        jobs.append(
            Job(
                source="arbeitnow",
                company=j.get("company_name", ""),
                title=j.get("title", ""),
                url=j.get("url", ""),
                location=loc,
                workplace="remote" if remote else infer_workplace(loc),
                description=clean_html(j.get("description", "")),
                posted_at=_date(j.get("created_at")),
                raw=j,
            )
        )
    return jobs


def _jobicy_rows(data) -> list[Job]:
    return [
        Job(
            source="jobicy",
            company=j.get("companyName", ""),
            title=j.get("jobTitle", ""),
            url=j.get("url", ""),
            location=j.get("jobGeo", "") or "Remote",
            workplace="remote",
            description=clean_html(j.get("jobExcerpt", "") + " " + j.get("jobDescription", "")),
            salary=str(j.get("annualSalaryMin") or ""),
            posted_at=_date(j.get("pubDate")),
            raw=j,
        )
        for j in data.get("jobs", [])
    ]


def _jobicy(client, cfg=None) -> list[Job]:
    """Global feed plus per-region feeds, so one region cannot crowd out another."""
    jobs = _jobicy_rows(_get(client, "https://jobicy.com/api/v2/remote-jobs?count=100"))
    for geo in (cfg or {}).get("geos") or []:
        try:
            jobs += _jobicy_rows(_get(
                client, f"https://jobicy.com/api/v2/remote-jobs?count=100&geo={quote(geo)}"
            ))
        except (httpx.HTTPError, ValueError):
            continue
    return jobs


MCF_SEARCH = "https://api.mycareersfuture.gov.sg/v2/search"
MCF_JOB = "https://api.mycareersfuture.gov.sg/v2/jobs/{uuid}"
MCF_EP_FLOOR = 5600            # overridden from profile.yaml by the caller


def _mcf_salary_note(salary: dict | None, floor: float) -> tuple[str, str]:
    """Say plainly whether the pay clears MOM's Employment Pass floor.

    This is the whole point of using the government feed: no other Singapore
    source states salary, and salary is what decides whether an Employment
    Pass can be granted at all. The sentence is written into the description
    so both the rules engine and the model can read it.
    """
    if not salary:
        return "", ""
    lo, hi = salary.get("minimum"), salary.get("maximum")
    kind = ((salary.get("type") or {}).get("salaryType") or "").lower()
    if not hi or kind != "monthly":
        return "", ""
    verdict = "meets" if hi >= floor else "below"
    band = f"S${lo:,}-{hi:,}" if lo else f"S${hi:,}"
    return verdict, (
        f"\n\n[work authorisation: monthly salary {band} {verdict} the "
        f"Employment Pass floor of S${floor:,.0f}. "
        + ("An EP is legally possible for this role."
           if verdict == "meets" else
           "No employer can sponsor an EP below the floor.")
        + "]"
    )


def _mycareersfuture(client, cfg=None) -> list[Job]:
    """Singapore's official government job board (MyCareersFuture, run by WSG).

    Public JSON API, no key. It is the only Singapore source that reports
    salary, which is what decides Employment Pass eligibility, so each posting
    is annotated with whether it clears MOM's floor.
    """
    cfg = cfg or {}
    terms = cfg.get("terms") or ["QA engineer"]
    per_term = int(cfg.get("per_term", 30))
    floor = float(cfg.get("ep_min_monthly_sgd", MCF_EP_FLOOR))
    want_desc = bool(cfg.get("fetch_descriptions", True))

    jobs: list[Job] = []
    seen: set[str] = set()
    for term in terms:
        try:
            resp = client.post(
                f"{MCF_SEARCH}?limit={per_term}&page=0",
                json={"search": term, "sessionId": "", "categories": []},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            results = resp.json().get("results") or []
        except (httpx.HTTPError, ValueError):
            continue

        for j in results:
            uuid = j.get("uuid")
            if not uuid or uuid in seen:
                continue
            seen.add(uuid)
            meta = j.get("metadata") or {}
            company = ((j.get("postedCompany") or {}).get("name")
                       or (j.get("hiringCompany") or {}).get("name") or "")

            _, note = _mcf_salary_note(j.get("salary"), floor)
            fwa = " ".join(
                str(f.get("flexibleWorkArrangement") if isinstance(f, dict) else f)
                for f in (j.get("flexibleWorkArrangements") or [])
            )
            remote = "remote" in fwa.lower() or "telecommut" in fwa.lower()

            jobs.append(Job(
                source="mycareersfuture",
                company=company,
                title=j.get("title", ""),
                url=meta.get("jobDetailsUrl") or "",
                location="Singapore",
                workplace="remote" if remote else "onsite",
                description=note,          # body filled in below
                salary=str((j.get("salary") or {}).get("maximum") or ""),
                posted_at=_date(meta.get("newPostingDate")),
                raw=j,
            ))

    # Descriptions come from a per-job endpoint, and the domain check needs
    # them - a Singapore "QA Engineer" is only identifiable as software or
    # shop-floor work from its body. Fetched concurrently: done one at a time
    # this was minutes of wall clock and stalled the daily run.
    if want_desc and jobs:
        def _describe(job):
            uuid = (job.raw or {}).get("uuid")
            if not uuid:
                return
            try:
                d = client.get(MCF_JOB.format(uuid=uuid), timeout=30)
                if d.status_code == 200:
                    body = clean_html(d.json().get("description") or "")
                    job.description = body + job.description
            except (httpx.HTTPError, ValueError):
                pass

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(_describe, jobs))
    return jobs


def _workingnomads(client, cfg=None) -> list[Job]:
    """Small curated remote board; one unpaginated endpoint, ~40 live postings.

    Low yield for a QA profile - most of what it carries is generic
    development, and its listings are usually region-locked to the EU or North
    America, which the location rule then rejects. Kept because it is one cheap
    request and occasionally carries an APAC-eligible role.
    """
    data = _get(client, "https://www.workingnomads.com/api/exposed_jobs/")
    return [
        Job(
            source="workingnomads",
            company=j.get("company_name", ""),
            title=j.get("title", ""),
            url=j.get("url", ""),
            location=j.get("location", "") or "Remote",
            workplace="remote",
            description=clean_html(j.get("description", "")),
            salary="",
            posted_at=_date(j.get("pub_date")),
            raw=j,
        )
        for j in (data if isinstance(data, list) else [])
    ]


def _himalayas(client, cfg=None) -> list[Job]:
    """Remote-first board with worldwide coverage and salary data. Paginated."""
    limit = 100
    want = int((cfg or {}).get("max_jobs", 400))
    jobs: list[Job] = []
    offset = 0
    while len(jobs) < want:
        data = _get(
            client,
            f"https://himalayas.app/jobs/api?limit={limit}&offset={offset}",
        )
        rows = data.get("jobs") or []
        if not rows:
            break
        for j in rows:
            loc = ", ".join(j.get("locationRestrictions") or []) or "Remote"
            lo, hi = j.get("minSalary"), j.get("maxSalary")
            salary = (
                f"{lo}-{hi} {j.get('currency') or ''}".strip() if lo or hi else ""
            )
            jobs.append(
                Job(
                    source="himalayas",
                    company=j.get("companyName", ""),
                    title=j.get("title", ""),
                    url=j.get("applicationLink") or j.get("guid") or "",
                    location=loc,
                    workplace="remote",
                    description=clean_html(
                        j.get("description") or j.get("excerpt") or ""
                    ),
                    salary=salary,
                    posted_at=_date(j.get("pubDate") or j.get("publishedDate")),
                    raw=j,
                )
            )
        offset += limit
        if offset >= int(data.get("totalCount") or 0):
            break
    return jobs


def _weworkremotely(client, cfg=None) -> list[Job]:
    """RSS categories. No JSON API, but the feeds are public and documented."""
    import feedparser

    categories = (cfg or {}).get("categories") or [
        "remote-programming-jobs",
    ]
    jobs: list[Job] = []
    for cat in categories:
        try:
            resp = client.get(
                f"https://weworkremotely.com/categories/{cat}.rss",
                headers=UA, timeout=TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            continue
        for entry in feedparser.parse(resp.content).entries:
            # WWR titles are "Company: Role"
            raw_title = entry.get("title", "")
            company, _, title = raw_title.partition(":")
            if not title:
                company, title = "", raw_title
            jobs.append(
                Job(
                    source="weworkremotely",
                    company=company.strip(),
                    title=title.strip(),
                    url=entry.get("link", ""),
                    location=entry.get("region", "") or "Remote",
                    workplace="remote",
                    description=clean_html(entry.get("summary", "")),
                    posted_at=_date(entry.get("published")),
                    raw={k: str(v) for k, v in entry.items() if isinstance(v, (str, int))},
                )
            )
    return jobs


def _adzuna_rows(data) -> list[Job]:
    jobs = []
    for j in data.get("results", []):
        loc = (j.get("location") or {}).get("display_name", "")
        desc = clean_html(j.get("description", ""))
        lo, hi = j.get("salary_min"), j.get("salary_max")
        salary = f"{lo:.0f}-{hi:.0f}" if lo and hi else (f"{lo:.0f}" if lo else "")
        jobs.append(
            Job(
                source="adzuna",
                company=(j.get("company") or {}).get("display_name", ""),
                title=j.get("title", ""),
                url=j.get("redirect_url", ""),
                location=loc,
                workplace=infer_workplace(loc, desc[:600]),
                description=desc,
                salary=salary,
                posted_at=_date(j.get("created")),
                raw=j,
            )
        )
    return jobs


def _adzuna(client, cfg: dict) -> list[Job]:
    """Adzuna search API, one query per (country x role term).

    Adzuna has no "give me everything" endpoint — it is search-driven, so the
    role terms below ARE the coverage. `where` narrows to a city, which is how
    Hyderabad onsite roles get found; without it Indian results are dominated
    by Bengaluru and NCR.
    """
    country = cfg.get("country", "in")
    terms = cfg.get("search_terms") or ["qa engineer"]
    wheres = cfg.get("where", {}).get(country) or [""]
    per_page = int(cfg.get("results_per_page", 50))
    pages = int(cfg.get("pages", 2))
    max_days = int(cfg.get("max_days_old", 7))

    jobs: list[Job] = []
    for term in terms:
        for where in wheres:
            for page in range(1, pages + 1):
                url = (
                    f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
                    f"?app_id={quote(str(cfg['app_id']))}"
                    f"&app_key={quote(str(cfg['app_key']))}"
                    f"&results_per_page={per_page}"
                    f"&what={quote(term)}"
                    f"&max_days_old={max_days}"
                    f"&content-type=application/json"
                )
                if where:
                    url += f"&where={quote(where)}"
                try:
                    data = _get(client, url)
                except (httpx.HTTPError, ValueError):
                    break            # bad key or rate limit: stop this branch
                rows = _adzuna_rows(data)
                jobs.extend(rows)
                if len(rows) < per_page:
                    break            # last page for this query
    return jobs


def fetch_feeds(sources: dict) -> tuple[list[Job], list[str]]:
    """Returns (jobs, errors). One dead feed never kills the run."""
    jobs: list[Job] = []
    errors: list[str] = []
    # (config key, handler, takes per-source config)
    handlers = [
        ("remoteok", _remoteok, False),
        ("remotive", _remotive, True),
        ("arbeitnow", _arbeitnow, False),
        ("jobicy", _jobicy, True),
        ("himalayas", _himalayas, True),
        ("weworkremotely", _weworkremotely, True),
        ("workingnomads", _workingnomads, False),
        ("mycareersfuture", _mycareersfuture, True),
    ]
    with httpx.Client(follow_redirects=True) as client:
        for name, fn, takes_cfg in handlers:
            cfg = sources.get(name)
            if not cfg:                       # false, absent, or empty
                continue
            try:
                jobs.extend(
                    fn(client, cfg if isinstance(cfg, dict) else {})
                    if takes_cfg else fn(client)
                )
            except Exception as exc:  # a flaky third-party feed is not fatal
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

        mail = sources.get("mailbox") or {}
        if mail.get("enabled"):
            from .mailbox import fetch_mailbox
            mail_jobs, mail_errors = fetch_mailbox(mail)
            jobs.extend(mail_jobs)
            errors.extend(mail_errors)

        adz = sources.get("adzuna") or {}
        if adz.get("enabled") and adz.get("app_id") and adz.get("app_key"):
            for country in adz.get("countries") or [adz.get("country", "in")]:
                try:
                    jobs.extend(_adzuna(client, {**adz, "country": country}))
                except Exception as exc:
                    errors.append(f"adzuna/{country}: {type(exc).__name__}: {exc}")

    return jobs, errors
