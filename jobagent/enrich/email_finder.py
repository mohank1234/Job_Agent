"""Verified work-email lookup by person name and company domain.

Uses the Apify actor clearpath/email-finder-api (checked 2026-09-28: 130k+
runs; $0.005 per run plus $0.008 per address pattern tested). It tests the
likely address patterns against the company's mail server and reports
whether each is deliverable. It does not read LinkedIn or any other site.

An address is returned only when the service marks it deliverable and safe
to send, and it is neither a catch-all domain (where any address "exists")
nor a role inbox. Everything else is treated as not found.

Requires APIFY_API_KEY. Every call passes maxTotalChargeUsd so Apify itself
caps the spend of that run.
"""
from __future__ import annotations

import os

import httpx

from ._errors import AdapterError, classify_http_error

ACTOR = "clearpath~email-finder-api"
APIFY_URL = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
TIMEOUT = httpx.Timeout(310.0, connect=10.0)
# The actor's free plan returns at most 5 emails per run; batches stay within it.
BATCH = 5

EmailFinderError = AdapterError


def usable(item):
    return bool(item.get("email") and item.get("isDeliverable") and item.get("isSafeToSend")
                and not item.get("isCatchAll") and not item.get("isRoleAccount") and not item.get("isDisposable"))


def find_emails(people, *, max_charge_per_run_usd=0.5, post=None):
    """people: [{firstName, surname, domain}]. Returns {(first, last): item}
    (lower-cased names) for verified results only; the address's own domain
    is checked by the caller. Raises EmailFinderError when a call fails."""
    api_key = os.environ.get("APIFY_API_KEY", "")
    if not api_key:
        raise EmailFinderError("unauthorized", "APIFY_API_KEY is not set")
    post = post or httpx.post
    found = {}
    for start in range(0, len(people), BATCH):
        batch = people[start:start + BATCH]
        try:
            resp = post(APIFY_URL, params={"token": api_key, "maxTotalChargeUsd": str(max_charge_per_run_usd)},
                        json={"people": batch}, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            if isinstance(exc, EmailFinderError):
                raise
            raise classify_http_error(exc, "Apify email finder") from exc
        if not isinstance(data, list):
            raise EmailFinderError("invalid_response", f"Email finder response was not a list: {type(data).__name__}")
        for item in data:
            if usable(item):
                found[((item.get("firstName") or "").casefold(), (item.get("surname") or "").casefold())] = item
    return found
