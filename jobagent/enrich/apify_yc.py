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
TIMEOUT = httpx.Timeout(120.0, connect=10.0)

ApifyError = AdapterError


def fetch_yc_jobs(queries, *, max_items=40, max_total_charge_usd=0.15, remote_only=False):
    """Return raw Apify job dicts (id, title, company, location, description,
    isRemote, salary, ...). Raises ApifyError on failure - an empty list
    must never silently mean "the call broke"."""
    api_key = os.environ.get("APIFY_API_KEY", "")
    if not api_key:
        raise ApifyError("unauthorized", "APIFY_API_KEY is not set")
    try:
        resp = httpx.post(
            APIFY_URL,
            params={"token": api_key, "maxTotalChargeUsd": str(max_total_charge_usd)},
            json={"queries": list(queries), "maxItems": max_items, "remoteOnly": remote_only},
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
