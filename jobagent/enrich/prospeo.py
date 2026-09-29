"""Prospeo Enrich Person - step 2 of the email waterfall (free plan).

https://prospeo.io/api-docs/enrich-person (checked 2026-09-29):
POST https://api.prospeo.io/enrich-person, header X-KEY, body
{"only_verified_email": true, "data": {"first_name", "last_name",
"company_website"}}. 1 credit per email found; no charge when nothing
matches ("NO_MATCH") or when the same record is re-enriched within 90 days.
Errors: INSUFFICIENT_CREDITS, INVALID_API_KEY, INVALID_DATAPOINTS, ...

Needs the person's name: it is used only after the decision-maker has been
identified. Only an email with status VERIFIED on the company's own domain
is accepted. Off unless PROSPEO_API_KEY is set.
"""
from __future__ import annotations

import os

import httpx

from jobagent.enrich.credits import LimitReached
from jobagent.outreach import founders

URL = "https://api.prospeo.io/enrich-person"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
REFUSALS = {"INSUFFICIENT_CREDITS", "INVALID_API_KEY", "RATE_LIMITED", "FORBIDDEN", "UNAUTHORIZED"}


def api_key():
    return os.environ.get("PROSPEO_API_KEY", "").strip()


def account_status(get=httpx.get):
    """{'left': credits left, 'reset': renewal date} from Prospeo's free
    account-information call, or {} when it cannot be read."""
    from datetime import date
    resp = get("https://api.prospeo.io/account-information", timeout=TIMEOUT,
               headers={"X-KEY": api_key(), "Content-Type": "application/json"})
    info = (resp.json() or {}).get("response") if resp.status_code == 200 else None
    if not isinstance(info, dict):
        return {}
    try:
        left = int(info.get("remaining_credits"))
        reset = date.fromisoformat(str(info.get("next_quota_renewal_date") or "")[:10])
    except (TypeError, ValueError):
        return {}
    return {"left": left, "reset": reset}


def find_email(first, last, domain, credits, post=httpx.post):
    """(email, detail) for a VERIFIED address on `domain`, else ('', reason).
    Raises LimitReached when the allowance is used or Prospeo refuses."""
    domain = founders.professional_domain(domain)
    if not (api_key() and first and last and domain):
        return "", "missing key, name or domain"
    credits.check()
    resp = post(URL, headers={"X-KEY": api_key(), "Content-Type": "application/json"}, timeout=TIMEOUT,
                json={"only_verified_email": True,
                      "data": {"first_name": first, "last_name": last, "company_website": domain}})
    try:
        data = resp.json()
    except ValueError:
        data = {}
    code = str(data.get("error_code") or "").upper()
    if resp.status_code in (401, 402, 403, 429) or code in REFUSALS:
        raise LimitReached(f"Prospeo refused the request ({code or resp.status_code})")
    if code == "NO_MATCH" or data.get("error") and not code:
        return "", "no match (no credit used)"
    if resp.status_code != 200 or data.get("error"):
        return "", f"unusable response ({code or resp.status_code})"
    email_obj = (data.get("person") or {}).get("email") or data.get("email") or {}
    email = email_obj.get("email") if isinstance(email_obj, dict) else ""
    status = str(email_obj.get("status") or "").upper() if isinstance(email_obj, dict) else ""
    if email:
        credits.spend()  # Prospeo charges when an email is returned
    if status == "VERIFIED" and founders.professional_email(email or "", domain):
        return email, f"VERIFIED ({email_obj.get('verification_method') or 'Prospeo'})"
    return "", f"returned {status or 'no'} email, not a verified address on {domain}"
