"""Apollo.io - optional second source for founder / CTO / CEO emails.

Off unless APOLLO_API_KEY is set. https://docs.apollo.io (checked
2026-09-28): People API Search (POST /api/v1/mixed_people/api_search) costs
0 credits and filters by company domain and seniority, but returns no email;
People Enrichment (POST /api/v1/people/match) reveals it and uses a credit.
Apollo requires an account registered with a work email for API search.

Zero cost: only the best-ranked leader is revealed, a local monthly counter
(`monthly_limit`) stops before the free allowance, and any refusal from
Apollo (no credits, plan limits) ends Apollo lookups for the run. On a free
plan with no card Apollo cannot bill. Only addresses Apollo marks
"verified" are used.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ._errors import AdapterError, classify_http_error
from jobagent.runtime import atomic_json, process_lock
from jobagent.outreach import founders

API = "https://api.apollo.io/api/v1"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
ApolloError = AdapterError


def api_key():
    return os.environ.get("APOLLO_API_KEY", "").strip()


def _headers():
    return {"x-api-key": api_key(), "Content-Type": "application/json", "Cache-Control": "no-cache"}


def _usage(path):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}
        if not isinstance(data, dict) or not isinstance(data.get("reveals", 0), int) or data.get("reveals", 0) < 0:
            raise ValueError("Invalid usage counter")
    except (OSError, ValueError) as exc:
        raise ApolloError("free_limit_reached", "Apollo usage counter unreadable; lookups disabled") from exc
    return data if data.get("month") == month else {"month": month, "reveals": 0}


def _refused(resp, data=None):
    if resp.status_code in (401, 402, 403, 422, 429):
        raise ApolloError("free_limit_reached", f"Apollo refused the request (HTTP {resp.status_code})")
    if isinstance(data, dict):
        error = " ".join(str(data.get(k) or "") for k in ("error", "errors", "message", "error_code"))
        if re.search(r"credit|plan|quota|rate.?limit|limit.{0,15}(?:exceed|reach)|payment|upgrade", error, re.I):
            raise ApolloError("free_limit_reached", "Apollo reported a credit, quota or plan restriction")


def _request(endpoint, payload, post):
    try:
        resp = post(f"{API}/{endpoint}", headers=_headers(), timeout=TIMEOUT, json=payload)
        _refused(resp)
        # Some credit/plan failures are returned with a JSON error and HTTP 200.
        data = resp.json()
        _refused(resp, data)
        resp.raise_for_status()
        if not isinstance(data, dict) or data.get("error") or data.get("errors"):
            raise ApolloError("invalid_response", "Apollo returned an unexpected response")
        return data
    except ApolloError:
        raise
    except Exception as exc:
        raise classify_http_error(exc, "Apollo") from exc


def leaders(domain, post=None):
    """Founders, owners and C-suite at a company domain (0 credits, no emails)."""
    domain = founders.professional_domain(domain)
    if not api_key() or not domain:
        return []
    data = _request("mixed_people/api_search",
                    {"q_organization_domains_list": [domain], "person_seniorities": ["founder", "owner", "c_suite", "vp", "head"],
                     "person_titles": ["Founder", "Co-founder", "CTO", "Chief Technology Officer", "CEO",
                                       "Chief Executive Officer", "VP Engineering", "Head of Engineering"],
                     "page": 1, "per_page": 25}, post or httpx.post)
    people = []
    for p in data.get("people") or []:
        name = p.get("name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
        organization = p.get("organization") or {}
        current_domain = founders.professional_domain(organization.get("primary_domain") or organization.get("website_url"))
        # Apollo's domain search may include previous employers. Exclude a
        # different current employer when one is disclosed by search.
        if current_domain and current_domain != domain:
            continue
        if (p.get("id") or p.get("person_id")) and name and founders.is_decision_maker(p.get("title")):
            people.append({"id": p.get("id") or p["person_id"], "name": name, "title": p["title"],
                           "linkedin": p.get("linkedin_url") or "", "domain": domain,
                           "organization": organization.get("name") or ""})
    return people


def reveal(person, domain, usage_path, monthly_limit=50, post=None):
    """The person's verified work email via People Enrichment (one credit), or ''."""
    domain = founders.professional_domain(domain)
    if not api_key() or not domain or not person.get("id") or not founders.is_decision_maker(person.get("title")):
        return ""
    usage_path = Path(usage_path)
    with process_lock(usage_path.with_suffix(".lock")):
        usage = _usage(usage_path)
        if usage.get("reveals", 0) >= max(0, int(monthly_limit)):
            raise ApolloError("free_limit_reached", f"Apollo reveals used for {usage['month']}")
        # Reserve before the request, conservatively counting an uncertain
        # timeout or crash. Never spend an unrecorded second credit on retry.
        usage["reveals"] = usage.get("reveals", 0) + 1
        atomic_json(usage_path, usage)
    data = _request("people/match", {"id": person["id"], "domain": domain,
                                    "reveal_personal_emails": False, "reveal_phone_number": False,
                                    "run_waterfall_email": False, "run_waterfall_phone": False}, post or httpx.post)
    found = data.get("person") or {}
    email = found.get("email") or ""
    current_org = found.get("organization") or {}
    current_domain = founders.professional_domain(current_org.get("primary_domain") or current_org.get("website_url"))
    if current_domain and current_domain != domain:
        return ""
    if found.get("id") and found["id"] != person["id"]:
        return ""
    if found.get("email_status") == "verified" and founders.professional_email(email, domain):
        # Search may hide last names. Replace its abbreviated identity with
        # the enriched identity, while never promoting a non-leadership role.
        title = found.get("title") or person.get("title")
        if not founders.is_decision_maker(title):
            return ""
        person.update(name=found.get("name") or " ".join(x for x in (found.get("first_name"), found.get("last_name")) if x)
                      or person.get("name"), title=title, linkedin=found.get("linkedin_url") or person.get("linkedin", ""))
        return email
    return ""


def best_contact(company, domain, usage_path, monthly_limit=50, team_size=None):
    """Reveal only the highest-ranked leader, returning normalized evidence.

    The daily runner catches ApolloError and disables this provider for the
    remainder of its run on a refusal. No fallback person is revealed here.
    """
    domain = founders.professional_domain(domain)
    if not api_key() or not domain:
        return {}
    usage = _usage(usage_path)
    if usage.get("reveals", 0) >= max(0, int(monthly_limit)):
        raise ApolloError("free_limit_reached", f"Apollo reveals used for {usage['month']}")
    people = leaders(domain)
    if not people:
        return {}
    person = min(people, key=lambda p: founders.rank(p.get("title"), team_size))
    email = reveal(person, domain, usage_path, monthly_limit)
    if not email:
        return {}
    person.update(email=email, verified=True, domain=domain, organization=company, provider="Apollo",
                  source=person.get("linkedin") or "https://docs.apollo.io/reference/people-enrichment")
    return founders.contact_from_executives(company, [person], domain_given=True, team_size=team_size,
                                           expected_domain=domain, provider="Apollo")
