"""Hunter.io Email Finder - free plan, no card (50 credits a month as of
2026-09-28, shown on the dashboard).

https://hunter.io/api-documentation/v2 (checked 2026-09-28): a search that
finds no email is not charged; one that does uses the month's allowance;
past the limit Hunter answers 429 "usage limit reached" instead of billing.
Two independent stops keep this at zero cost regardless:
- a local monthly counter capped at `monthly_limit` (default 50), and
- Hunter's own account usage, read before every search when it reports it.

Only addresses Hunter has verified ("valid") are used; "accept_all"
(catch-all domain, where any address looks deliverable) and "unknown" are
treated as not found.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ._errors import AdapterError, classify_http_error

API = "https://api.hunter.io/v2"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

HunterError = AdapterError


def api_key():
    return os.environ.get("HUNTER_API_KEY", "").strip()


def _usage(path):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    data = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}
    return data if data.get("month") == month else {"month": month, "searches": 0}


def account_searches_left(key, get=httpx.get):
    """Searches left this month according to Hunter, or None if not reported."""
    try:
        data = get(f"{API}/account", params={"api_key": key}, timeout=TIMEOUT).json().get("data", {})
    except Exception:
        return None
    # Older plans report "searches"; current plans report shared "credits"
    # (the free plan shows 50 a month). Whichever is reported, the smaller wins.
    left = []
    for kind in ("searches", "credits"):
        counts = (data.get("requests") or {}).get(kind) or data.get(kind) or {}
        if isinstance(counts, dict) and "available" in counts and "used" in counts:
            left.append(max(0, int(counts["available"]) - int(counts["used"])))
    return min(left) if left else None


def _spend_check(usage_path, monthly_limit, key, get):
    import calendar
    import math
    usage = _usage(usage_path)
    now = datetime.now(timezone.utc)
    if usage.get("day") != now.strftime("%Y-%m-%d"):
        usage.update(day=now.strftime("%Y-%m-%d"), before_today=usage["searches"])
    if usage["searches"] >= monthly_limit:
        raise HunterError("free_limit_reached", f"Hunter free searches used for {usage['month']}")
    # Spread the month's free credits over the days left, best companies first each day.
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    allowance = math.ceil((monthly_limit - usage["before_today"]) / days_left)
    if usage["searches"] - usage["before_today"] >= allowance:
        raise HunterError("free_limit_reached", f"Hunter allowance for today ({allowance}) used")
    left = account_searches_left(key, get)
    if left is not None and left <= 0:
        raise HunterError("free_limit_reached", "Hunter reports no searches left this month")
    return usage


def _count(usage_path, usage):
    usage["searches"] += 1
    Path(usage_path).parent.mkdir(parents=True, exist_ok=True)
    Path(usage_path).write_text(json.dumps(usage), encoding="utf-8")


def company_size(domain, usage_path, monthly_limit=50, get=httpx.get):
    """(low, high) employees from Hunter Company Enrichment ("11-50" or a count),
    or None. Counted as one credit whenever Hunter returns the company."""
    key = api_key()
    usage = _spend_check(usage_path, monthly_limit, key, get)
    try:
        resp = get(f"{API}/companies/find", params={"domain": domain, "api_key": key}, timeout=TIMEOUT)
        if resp.status_code == 429:
            raise HunterError("free_limit_reached", "Hunter usage limit reached")
        if resp.status_code >= 400:
            return None
        data = resp.json().get("data") or {}
    except HunterError:
        raise
    except Exception:
        return None
    if data:
        _count(usage_path, usage)
    metrics = data.get("metrics") or {}
    count = metrics.get("employeesCount")
    if isinstance(count, (int, float)) and count > 0:
        return int(count), int(count)
    band = re.fullmatch(r"\s*([\d,]+)\s*[-–]\s*([\d,]+)\s*", str(metrics.get("employees") or ""))
    if band:
        return int(band.group(1).replace(",", "")), int(band.group(2).replace(",", ""))
    return None


def executives(company, domain, usage_path, monthly_limit=50, get=httpx.get):
    """Domain Search for a company's executives: [{name, title, email, verified,
    linkedin, source, domain}]. Uses one credit only when people come back.
    Works from the company name alone when its website is unknown."""
    key = api_key()
    usage = _spend_check(usage_path, monthly_limit, key, get)
    params = {"api_key": key, "seniority": "executive", "type": "personal", "limit": 10}
    params.update({"domain": domain} if domain else {"company": company})
    try:
        resp = get(f"{API}/domain-search", params=params, timeout=TIMEOUT)
        if resp.status_code == 429:
            raise HunterError("free_limit_reached", "Hunter usage limit reached")
        if resp.status_code in (400, 404):
            return []
        resp.raise_for_status()
        data = resp.json().get("data") or {}
    except HunterError:
        raise
    except Exception as exc:
        raise classify_http_error(exc, "Hunter") from exc
    emails = data.get("emails") or []
    if emails:
        _count(usage_path, usage)
    found_domain = (data.get("domain") or domain or "").lower()
    people = []
    for e in emails:
        name = " ".join(p for p in (e.get("first_name"), e.get("last_name")) if p)
        source = next((s.get("uri") for s in e.get("sources") or [] if (s.get("uri") or "").startswith("http")), "")
        people.append({"name": name, "title": e.get("position") or "", "email": e.get("value") or "",
                       "verified": (e.get("verification") or {}).get("status") == "valid",
                       "linkedin": e.get("linkedin") or "", "source": source, "domain": found_domain,
                       "organization": data.get("organization") or "", "confidence": e.get("confidence")})
    return people


def make_finder(usage_path, monthly_limit=50, get=httpx.get):
    """A finder for founders.verified_contact(): [{firstName, surname, domain}]
    -> {(first, last): {email, validationStatus, overallScore}}; verified only."""
    key = api_key()

    def finder(people):
        found = {}
        for person in people:
            usage = _spend_check(usage_path, monthly_limit, key, get)
            try:
                resp = get(f"{API}/email-finder", timeout=TIMEOUT,
                           params={"domain": person["domain"], "first_name": person["firstName"],
                                   "last_name": person["surname"], "api_key": key})
                if resp.status_code == 429:
                    raise HunterError("free_limit_reached", "Hunter usage limit reached")
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json().get("data") or {}
            except HunterError:
                raise
            except Exception as exc:
                raise classify_http_error(exc, "Hunter") from exc
            if data.get("email"):
                # Counted only when an email came back: that is when Hunter charges.
                _count(usage_path, usage)
            status = (data.get("verification") or {}).get("status")
            if data.get("email") and status == "valid":
                found[(person["firstName"].casefold(), person["surname"].casefold())] = {
                    "email": data["email"], "validationStatus": "Hunter verified: valid",
                    "overallScore": data.get("score", "n/a")}
        return found

    return finder
