"""Tomba Email Finder - step 4 (last) of the email waterfall (free plan).

https://docs.tomba.io (checked 2026-09-29): GET
https://api.tomba.io/v1/email-finder?domain=&first_name=&last_name=, headers
X-Tomba-Key and X-Tomba-Secret; data.email, data.score and
data.verification.status ("valid" ...). Whether an empty result uses one of
the 25 free monthly searches is not documented, so every call is counted.

Only a "valid" address on the company's own domain is accepted. Off unless
TOMBA_API_KEY and TOMBA_SECRET are set.
"""
from __future__ import annotations

import os

import httpx

from jobagent.enrich.credits import LimitReached
from jobagent.outreach import founders

URL = "https://api.tomba.io/v1/email-finder"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def keys():
    return os.environ.get("TOMBA_API_KEY", "").strip(), os.environ.get("TOMBA_SECRET", "").strip()


def active():
    return all(keys())


def find_email(first, last, domain, credits, get=httpx.get):
    """(email, detail) for a verified address on `domain`, else ('', reason).
    Raises LimitReached when the allowance is used or Tomba refuses."""
    domain = founders.professional_domain(domain)
    if not (active() and first and last and domain):
        return "", "missing keys, name or domain"
    credits.check()
    key, secret = keys()
    credits.spend()  # counted before the call: Tomba's no-result policy is undocumented
    resp = get(URL, params={"domain": domain, "first_name": first, "last_name": last}, timeout=TIMEOUT,
               headers={"X-Tomba-Key": key, "X-Tomba-Secret": secret})
    if resp.status_code in (401, 402, 403, 429):
        raise LimitReached(f"Tomba refused the request (HTTP {resp.status_code})")
    if resp.status_code == 404:
        return "", "no match"
    try:
        data = (resp.json() or {}).get("data") or {}
    except ValueError:
        data = {}
    if resp.status_code != 200:
        return "", f"unusable response (HTTP {resp.status_code})"
    email = data.get("email") or ""
    status = str((data.get("verification") or {}).get("status") or "").lower()
    if status == "valid" and founders.professional_email(email, domain):
        return email, f"valid (score {data.get('score', 'n/a')})"
    return "", f"returned {status or 'no'} email, not a verified address on {domain}"
