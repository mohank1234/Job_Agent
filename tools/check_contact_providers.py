"""Check the email-provider keys without spending any credit.

Prospeo: POST /account-information (free) reports plan and credits left.
Hunter: GET /v2/account is free and reports the plan and its remaining
credits. Tomba: GET /v1/me (free) confirms the key pair. Apollo: one People
API Search (0 credits per Apollo's docs). No email lookup is made here.
Prints results only; never prints the keys.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check_hunter():
    key = os.environ.get("HUNTER_API_KEY", "").strip()
    if not key:
        return "Hunter: no HUNTER_API_KEY secret; founder emails from Hunter are off."
    resp = httpx.get("https://api.hunter.io/v2/account", params={"api_key": key}, timeout=30)
    if resp.status_code != 200:
        return f"Hunter: key refused (HTTP {resp.status_code}). Re-copy the key from hunter.io/api-keys."
    data = resp.json().get("data") or {}
    requests = data.get("requests") or {}
    parts = [f"{kind} {c.get('used', '?')}/{c.get('available', '?')} used"
             for kind, c in requests.items() if isinstance(c, dict)]
    return (f"Hunter: OK - plan {data.get('plan_name', '?')}; {', '.join(parts) or 'usage not reported'}; "
            f"free credits renew on {data.get('reset_date') or 'an unreported date'}.")


def check_apollo():
    key = os.environ.get("APOLLO_API_KEY", "").strip()
    if not key:
        return "Apollo: no APOLLO_API_KEY secret; Apollo is off (optional)."
    resp = httpx.post("https://api.apollo.io/api/v1/mixed_people/api_search", timeout=30,
                      headers={"x-api-key": key, "Content-Type": "application/json", "Cache-Control": "no-cache"},
                      json={"q_organization_domains_list": ["apollo.io"], "person_seniorities": ["c_suite"],
                            "page": 1, "per_page": 1})
    if resp.status_code == 200:
        people = (resp.json().get("people") or [])
        return (f"Apollo: OK - People API Search works on this plan (0 credits used; {len(people)} sample result). "
                "Email reveals will use the plan's free credits, capped by apollo_monthly_limit.")
    try:
        detail = str(resp.json())[:200]
    except ValueError:
        detail = resp.text[:200]
    hints = {401: "the key is wrong or deleted - create it again",
             403: "the key lacks access to this endpoint, or the plan has no API access - "
                  "recreate it with 'Set as master key' on, or check Apollo's plan",
             422: "Apollo rejected the request - often a plan or account restriction",
             429: "rate limited - try again later"}
    return f"Apollo: not usable (HTTP {resp.status_code}: {hints.get(resp.status_code, 'unexpected response')}). {detail}"


def check_prospeo():
    key = os.environ.get("PROSPEO_API_KEY", "").strip()
    if not key:
        return "Prospeo: no PROSPEO_API_KEY secret; Prospeo is off."
    resp = httpx.post("https://api.prospeo.io/account-information", timeout=30,
                      headers={"X-KEY": key, "Content-Type": "application/json"})
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or data.get("error"):
        return f"Prospeo: key not usable (HTTP {resp.status_code}): {str(data)[:200]}"
    info = data.get("response") or data
    return (f"Prospeo: OK - plan {info.get('current_plan', '?')}; {info.get('remaining_credits', '?')} credits left"
            f"{'; renews ' + str(info.get('next_quota_renewal_date')) if info.get('next_quota_renewal_date') else ''}.")


def check_tomba():
    key, secret = os.environ.get("TOMBA_API_KEY", "").strip(), os.environ.get("TOMBA_SECRET", "").strip()
    if not (key and secret):
        return "Tomba: TOMBA_API_KEY and TOMBA_SECRET secrets not both set; Tomba is off."
    resp = httpx.get("https://api.tomba.io/v1/me", timeout=30, headers={"X-Tomba-Key": key, "X-Tomba-Secret": secret})
    try:
        data = (resp.json() or {}).get("data") or {}
    except ValueError:
        data = {}
    if resp.status_code != 200:
        return f"Tomba: keys not usable (HTTP {resp.status_code}): {resp.text[:200]}"
    return f"Tomba: OK - account {'found' if data else 'response received'}; usage details: {str(data.get('requests') or data.get('usage') or '')[:160]}"


def check_apollo_enrichment():
    """One People Enrichment lookup by name + domain (uses 1 free credit if a
    match is returned; the free plan cannot bill). Only run on request."""
    key = os.environ.get("APOLLO_API_KEY", "").strip()
    if not key:
        return "Apollo enrichment: no APOLLO_API_KEY secret."
    resp = httpx.post("https://api.apollo.io/api/v1/people/match", timeout=30,
                      headers={"x-api-key": key, "Content-Type": "application/json", "Cache-Control": "no-cache"},
                      json={"first_name": "Tim", "last_name": "Zheng", "domain": "apollo.io",
                            "reveal_personal_emails": False, "reveal_phone_number": False})
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200:
        return f"Apollo enrichment: not usable (HTTP {resp.status_code}): {str(data or resp.text)[:220]}"
    person = data.get("person") or {}
    if not person:
        return "Apollo enrichment: allowed on this plan, but the test person was not matched (no credit used)."
    email = person.get("email") or ""
    shown = (email[:2] + "***@" + email.split("@")[1]) if "@" in email else "none"
    return (f"Apollo enrichment: WORKS on this plan - matched {person.get('title') or 'person'} at "
            f"{(person.get('organization') or {}).get('name') or 'the company'}; email {shown}, "
            f"status {person.get('email_status') or 'unknown'}.")


if __name__ == "__main__":
    checks = [check_prospeo(), check_hunter(), check_tomba(), check_apollo()]
    if "--apollo-enrichment" in sys.argv:
        checks.append(check_apollo_enrichment())
    for line in checks:
        print(line)
