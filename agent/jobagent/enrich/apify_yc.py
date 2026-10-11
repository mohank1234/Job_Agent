"""Apify actor adapter - Y Combinator "Work at a Startup" live job board.

companies.yaml is a static, manually-curated list of ~168 ATS board slugs;
it has no way to discover new YC companies or their current openings on its
own. Work at a Startup has no public API of its own (checked 2026-09-20),
so the only documented, paid route to its 1,000+ live postings is via an
Apify actor - here, nomad-agent/ycombinator-was-scraper (id
1JDUBKElJkIKoCkgG), verified working with real matching results.

Requires APIFY_API_KEY. Pay-per-event pricing (checked 2026-09-20: $0.0025
per job returned on the free account tier). Every call passes
maxTotalChargeUsd so Apify itself enforces a hard spend cap server-side -
this adapter never trusts a locally-tracked "remaining budget" number, since
nothing in this API exposes the account's actual remaining credit.
"""
from __future__ import annotations

import os

import httpx

from ._errors import AdapterError, classify_http_error

ACTOR_ID = "1JDUBKElJkIKoCkgG"  # nomad-agent/ycombinator-was-scraper
APIFY_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"
# A full (uncapped) result set takes longer than a 40-item page; Apify holds
# a synchronous run open for up to 300 seconds.
TIMEOUT = httpx.Timeout(310.0, connect=10.0)

ApifyError = AdapterError


# The actor's input (schema v2, checked 2026-09-28): at most 20 queries, each
# matching postings that contain every word of it; maxItems above 200 means
# 200. "dedupe" is on by default and hides postings already delivered to this
# account for the same search, so a repeat run returned 5 postings instead of
# 49 and still-open jobs vanished from the report. It is always turned off.
MAX_QUERIES = 20
MAX_ITEMS = 200


def fetch_yc_jobs(queries, *, max_items=MAX_ITEMS, max_total_charge_usd=0.6):
    """Return raw Apify job dicts (id, url, title, company, locations,
    workType, salary, description, hiringContacts, ...). Raises ApifyError
    on failure - an empty list must never silently mean "the call broke"."""
    api_key = os.environ.get("APIFY_API_KEY", "")
    if not api_key:
        raise ApifyError("unauthorized", "APIFY_API_KEY is not set")
    try:
        resp = httpx.post(
            APIFY_URL,
            params={"token": api_key, "maxTotalChargeUsd": str(max_total_charge_usd)},
            json={"schemaVersion": "nomad-agent-simple-inventory-search-v2",
                  "queries": list(queries)[:MAX_QUERIES], "maxItems": min(max_items, MAX_ITEMS),
                  "dedupe": {"enabled": False, "key": ""}},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        if isinstance(exc, ApifyError):
            raise
        raise classify_http_error(exc, "Apify") from exc
    if not isinstance(data, list):
        raise ApifyError("invalid_response", f"Apify response was not a list: {type(data).__name__}")
    return data
