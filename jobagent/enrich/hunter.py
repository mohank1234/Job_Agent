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


def make_finder(usage_path, monthly_limit=50, get=httpx.get):
    """A finder for founders.verified_contact(): [{firstName, surname, domain}]
    -> {(first, last): {email, validationStatus, overallScore}}; verified only."""
    key = api_key()

    def finder(people):
        found = {}
        for person in people:
            usage = _usage(usage_path)
            if usage["searches"] >= monthly_limit:
                raise HunterError("free_limit_reached", f"Hunter free searches used for {usage['month']}")
            left = account_searches_left(key, get)
            if left is not None and left <= 0:
                raise HunterError("free_limit_reached", "Hunter reports no searches left this month")
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
                usage["searches"] += 1
                Path(usage_path).parent.mkdir(parents=True, exist_ok=True)
                Path(usage_path).write_text(json.dumps(usage), encoding="utf-8")
            status = (data.get("verification") or {}).get("status")
            if data.get("email") and status == "valid":
                found[(person["firstName"].casefold(), person["surname"].casefold())] = {
                    "email": data["email"], "validationStatus": "Hunter verified: valid",
                    "overallScore": data.get("score", "n/a")}
        return found

    return finder
