"""Check the Hunter and Apollo keys without spending any credit.

Hunter: GET /v2/account is free and reports the plan and its remaining
credits. Apollo: one People API Search (0 credits per Apollo's docs) for a
public domain shows whether the key, its endpoint access and the plan allow
API use. People Enrichment (the credit-using call) is not made here.
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
    checks = [check_hunter(), check_apollo()]
    if "--apollo-enrichment" in sys.argv:
        checks.append(check_apollo_enrichment())
    for line in checks:
        print(line)
