"""Exa search adapter — company, job, and professional-profile discovery.

Public documented API: https://docs.exa.ai/reference/search
Requires EXA_API_KEY (free tier: $20 signup credit + $10/month recurring,
no card needed, checked 2026-09-08 at https://exa.ai/pricing).
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from ._errors import AdapterError, classify_http_error

EXA_API_URL = "https://api.exa.ai/search"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

ExaError = AdapterError

# Free-credit guard. Exa's free plan resets to $10 of credit each month with
# no payment method (checked 2026-09-28 at https://exa.ai/pricing: $7 per
# 1,000 searches of up to 10 results, $1 per 1,000 extra results, $1 per 1,000
# pages of text). Once configure_budget() is called, every search is priced
# with that tariff and refused before the month's estimated spend would pass
# the limit, so usage stays inside the free credit even if a card is on file.
_BUDGET: dict = {}


def configure_budget(state_path, monthly_limit_usd):
    _BUDGET.clear()
    if state_path and monthly_limit_usd is not None:
        _BUDGET.update(path=Path(state_path), limit=float(monthly_limit_usd))


def search_cost(num_results: int) -> float:
    return 0.007 + 0.001 * max(0, num_results - 10) + 0.001 * num_results


def _charge(num_results: int):
    if not _BUDGET:
        return
    import json
    from datetime import datetime, timezone
    import calendar
    now = datetime.now(timezone.utc)
    month, day = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
    path = _BUDGET["path"]
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        # Spending earlier this month, before this counter existed, is unknown,
        # so the first month is treated as already used up. Counting starts
        # clean when the free credit resets on the 1st.
        data = {"month": month, "spent_usd": _BUDGET["limit"], "searches": 0, "prior_usage_unknown": True}
    if data.get("month") != month:
        data = {"month": month, "spent_usd": 0.0, "searches": 0}
    if data.get("day") != day:
        data.update(day=day, spent_before_today=data["spent_usd"])
    # Spread what is left evenly over the rest of the month, so one busy day
    # cannot leave the remaining days without any searches.
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    today_allowance = (_BUDGET["limit"] - data["spent_before_today"]) / days_left
    cost = search_cost(num_results)
    if (data["spent_usd"] + cost > _BUDGET["limit"]
            or data["spent_usd"] - data["spent_before_today"] + cost > today_allowance):
        _save(path, data)
        raise ExaError("free_budget_reached",
                       f"Exa free-credit allowance used for {day} (${data['spent_usd']:.2f} of "
                       f"${_BUDGET['limit']:.2f} this month); remaining searches wait for tomorrow")
    data["spent_usd"] = round(data["spent_usd"] + cost, 4)
    data["searches"] += 1
    _save(path, data)


def _save(path, data):
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def exa_search(
    query: str,
    num_results: int = 10,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_published_date: str | None = None,
    max_characters: int = 500,
) -> list[dict]:
    """Run a neural search query. Returns a list of
    {title, url, published_date, author, snippet}.

    Raises ExaError (AdapterError) with a `.kind` on failure — never returns
    an empty list to mean "something went wrong"; a genuinely empty result
    set and a broken key must not look the same to the caller.
    """
    api_key = os.environ.get("EXA_API_KEY", "")
    if not api_key:
        raise ExaError("unauthorized", "EXA_API_KEY is not set")
    # Counted before the call: a request that fails after sending may still be billed.
    _charge(num_results)

    body: dict = {"query": query, "numResults": num_results, "contents": {"text": {"maxCharacters": max_characters}}}
    if include_domains:
        body["includeDomains"] = include_domains
    if exclude_domains:
        body["excludeDomains"] = exclude_domains
    if start_published_date:
        body["startPublishedDate"] = start_published_date

    try:
        resp = httpx.post(
            EXA_API_URL,
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        if isinstance(exc, ExaError):
            raise
        raise classify_http_error(exc, "Exa") from exc

    if "results" not in data:
        raise ExaError("invalid_response", f"Exa response had no 'results' key: {list(data.keys())}")

    out = []
    for r in data["results"]:
        text = r.get("text") or ""
        out.append({
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "published_date": r.get("publishedDate"),
            "author": r.get("author"),
            "snippet": text[:max_characters],
        })
    return out
