"""Fetch evidence for outreach. Search snippets are never job descriptions.

Only supported public ATS responses can establish verified_live. Other pages
remain reviewable evidence, even when a scraper successfully extracts text.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from urllib.parse import urlparse, parse_qs

import httpx

from jobagent.models import clean_html
from jobagent.matcher import rule_match
from jobagent.roles import classify, remote_eligibility
from jobagent.runtime import now_iso
from jobagent.sources.ats import ADAPTERS
from .service import EMAIL

ATS_HOSTS = {"jobs.ashbyhq.com": "ashby", "jobs.lever.co": "lever",
             "boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse",
             "job-boards.eu.greenhouse.io": "greenhouse"}


def public_url(url):
    import ipaddress
    import socket
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError("Expected a public HTTPS URL without embedded credentials")
    for addr in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(addr[4][0]).is_global:
            raise ValueError("Private or loopback destinations are not supported")
    return url


def get_public(client, url):
    from urllib.parse import urljoin
    for _ in range(5):
        public_url(url)
        response = client.get(url, timeout=25, follow_redirects=False)
        if response.is_redirect:
            url = urljoin(url, response.headers["location"])
            continue
        response.raise_for_status()
        return response
    raise ValueError("Too many redirects")


def board_ref(url):
    parsed = urlparse(url)
    vendor = ATS_HOSTS.get((parsed.hostname or "").lower())
    parts = [part for part in parsed.path.split("/") if part]
    if not vendor or not parts or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]):
        return None
    return vendor, parts[0]


def canonical(url):
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host == "boards.greenhouse.io":
        host = "job-boards.greenhouse.io"
    path = p.path.rstrip("/")
    # ATS application forms and the posting describe the same requisition.
    if host in ATS_HOSTS and path.endswith("/application"):
        path = path[:-12]
    identity = parse_qs(p.query).get('gh_jid', [''])[0]
    return host + path + (('?gh_jid=' + identity) if identity.isdigit() and host not in ATS_HOSTS else '')


def employer_posting(job, vendor, slug):
    """Resolve employer-owned Greenhouse URLs using the actual API posting ID."""
    from dataclasses import replace
    if vendor != 'greenhouse' or board_ref(job.url):
        return job
    posting_id = str((job.raw or {}).get('id', ''))
    if not posting_id.isdigit():
        return job
    return replace(job, url=f'https://job-boards.greenhouse.io/{slug}/jobs/{posting_id}',
                   raw={**job.raw, 'employer_url': job.url})


def fetch_board(url, client):
    ref = board_ref(url)
    if not ref:
        return None, None
    vendor, slug = ref
    make_url, parse = ADAPTERS[vendor]
    api_url = make_url(slug)
    response = get_public(client, api_url)
    data = response.json()
    if vendor == "lever":
        if not isinstance(data, list):
            raise ValueError("Invalid Lever response")
    elif not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("ATS response lacks a jobs array")
    return [employer_posting(j, vendor, slug) for j in parse(slug, data)], api_url


def verify_row(row, profile, client, *, board_cache=None, use_firecrawl=False):
    out = dict(row)
    # Old text remains labeled as a claim; never recycle it into fresh evidence.
    out["Previous JD Claim"] = row.get("JD Text", "")
    out["Previous JD Status"] = row.get("JD Status", "")
    out.update({"JD Text": "", "JD Status": "exploratory", "JD Verified At": "",
                "Checked At": now_iso(), "JD Source URL": "", "JD SHA256": "",
                "Contact Verification": "unverified", "Contact Verified At": "",
                "Contact Source URL": "", "Fit Status": "unverified", "Fit Notes": "",
                "Content Status": "needs_review", "Verification Error": ""})
    url = row.get("Job Link", "").strip()
    cache = board_cache if board_cache is not None else {}
    try:
        if url:
            ref = board_ref(url)
            if not ref:
                out["JD Status"] = "unsupported_source"
                if use_firecrawl:
                    from jobagent.enrich.firecrawl import firecrawl_scrape
                    public_url(url)
                    page = firecrawl_scrape(url)
                    status = page["metadata"].get("statusCode")
                    if status is None or int(status) >= 400:
                        out["JD Status"] = "fetch_error"
                    else:
                        out["Extracted Page Text (unverified)"] = page["markdown"]
                # General careers pages and JS extraction are not proof of a
                # specific opening. Add an ATS Job Link to verify it.
            else:
                if ref not in cache:
                    try:
                        cache[ref] = fetch_board(url, client)
                    except Exception as exc:
                        # A blocked board is one failed request, not one per row.
                        cache[ref] = exc
                if isinstance(cache[ref], Exception):
                    raise cache[ref]
                jobs, api_url = cache[ref]
                matches = [j for j in jobs if canonical(j.url) == canonical(url)]
                out["JD Source URL"] = api_url
                if len(matches) != 1:
                    out["JD Status"] = "not_currently_listed" if not matches else "ambiguous_posting"
                else:
                    job = matches[0]
                    if len(job.description.strip()) < 200:
                        out["JD Status"] = "description_missing"
                    else:
                        out.update({"JD Status": "verified_live", "JD Text": job.description,
                                    "JD Verified At": now_iso(), "Job Title": job.title,
                                    "Location": job.location, "Workplace": job.workplace,
                                    "Employer Job Link": (job.raw or {}).get('employer_url', job.url),
                                    "Salary (source)": job.salary or "Not stated in structured source; check full JD",
                                    "JD SHA256": hashlib.sha256(job.description.encode()).hexdigest()})
                        job.apply_classification(classify(job, profile))
                        rule_match(job, profile)
                        fit = "in_scope" if job.candidate and job.match_category in ("A", "B", "C") else "out_of_scope"
                        caveats = list(job.classification_notes)
                        requirements = list(dict.fromkeys(re.findall(
                            r"\b\d{1,2}(?:\s*[-\u2013\u2014]\s*\d{1,2})?\+?(?:\s+or more)?\s+years?\b[^.\n]{0,130}", job.description, re.I)))
                        out["Experience Requirements (source excerpts)"] = " | ".join(requirements)
                        years = [int(re.match(r"\d+", text).group()) for text in requirements
                                 if re.search(r"experience|testing|automation|quality|QA|SDET", text, re.I)]
                        if years and max(years) > profile.years:
                            caveats.append(f"Posting includes a {max(years)}-year requirement; profile states approximately {profile.years:g} years. Review the full requirements.")
                            if fit == "in_scope":
                                fit = "needs_review"
                        if re.search(r"fixed.term|\bcontract\b", job.title, re.I):
                            caveats.append("Fixed-term/contract role; confirm duration, benefits and compensation")
                            if fit == "in_scope":
                                fit = "needs_review"
                        if job.workplace == "remote" and re.search(r"(?:office in|onsite|on-site|hybrid)", job.description, re.I):
                            caveats.append("Posting also mentions an office/onsite arrangement; confirm the role's remote terms")
                            if fit == "in_scope":
                                fit = "needs_review"
                        raw = job.raw or {}
                        workplace = str(raw.get("workplaceType", "")).lower()
                        if raw.get("isRemote") and workplace in ("hybrid", "onsite", "on-site"):
                            if fit == "in_scope":
                                fit = "needs_review"
                            caveats.append("ATS isRemote conflicts with workplaceType; confirm working arrangement")
                        if job.workplace == "remote" and remote_eligibility(job)[0] == "unspecified":
                            if fit == "in_scope":
                                fit = "needs_review"
                            caveats.append("India-based remote eligibility is unconfirmed")
                        # These are literal profile/JD overlaps, not an invented
                        # assertion that every requirement has been met.
                        mentioned = [s for s in profile.all_skills if re.search(
                            r"(?<!\w)" + re.escape(s) + r"(?!\w)", job.description, re.I)]
                        gaps = [s for s in profile.gaps if re.search(
                            r"(?<!\w)" + re.escape(s) + r"(?!\w)", job.description, re.I)]
                        if gaps:
                            caveats.append("Known profile gaps mentioned in JD: " + ", ".join(gaps))
                            if fit == "in_scope":
                                fit = "needs_review"
                        if "below current level" in " ".join(caveats):
                            if fit == "in_scope":
                                fit = "needs_review"
                        intermediary = ref[1].lower() == "jobgether" or re.search(
                            r"on behalf of (?:our |a )?(?:partner|client)|(?:our |a )partner company", job.description, re.I)
                        out["Listing Type"] = "Intermediary; employer needs verification" if intermediary else "Company ATS board"
                        if intermediary:
                            caveats.append("Listed by an intermediary; verify the actual employer and its own careers posting before applying")
                            if fit == "in_scope":
                                fit = "needs_review"
                        if re.search(r"\bmobile\b", job.title, re.I) and not any(
                                term in profile.all_skills for term in ("appium", "espresso", "maestro", "flutter")):
                            caveats.append("Mobile automation specialization is not established in the profile; review Appium/Flutter/Espresso requirements")
                            if fit == "in_scope":
                                fit = "needs_review"
                        requirement = re.search(
                            r"(?:required qualifications|requirements|what you.ll bring)\s*:?\s*(.{200,2000})",
                            job.description, re.I | re.S)
                        out["Requirements Excerpt (source)"] = requirement.group(0) if requirement else "Read the full JD; no standard requirements heading found"
                        out.update({"Fit Status": fit, "Fit Notes": "; ".join(caveats),
                                    "Profile Skills Mentioned": ", ".join(mentioned),
                                    "Known Gaps Mentioned": ", ".join(gaps),
                                    "Scoring Method": "Deterministic rules; full requirements need review",
                                    "Rule Score": str(job.score)})
    except httpx.HTTPStatusError as exc:
        out["JD Status"] = "blocked" if exc.response.status_code in (401, 403, 429) else "fetch_error"
        out["Verification Error"] = f"HTTP {exc.response.status_code}"
    except Exception as exc:
        out["JD Status"] = "fetch_error"
        out["Verification Error"] = type(exc).__name__

    # Exact public email evidence on a company-owned source. This does not
    # assert deliverability or that a shared mailbox belongs to a named person.
    email = row.get("Email", "").strip()
    contact_source = row.get("Contact Source URL", "").strip()
    company_site = row.get("Company Website", "").strip()
    if EMAIL.fullmatch(email) and contact_source and company_site:
        source_host = (urlparse(contact_source).hostname or "").lower().removeprefix("www.")
        company_host = (urlparse(company_site).hostname or "").lower().removeprefix("www.")
        if company_host and (source_host == company_host or source_host.endswith("." + company_host)):
            try:
                page = get_public(client, contact_source)
                final_host = (page.url.host or "").lower().removeprefix("www.")
                if final_host != company_host and not final_host.endswith("." + company_host):
                    raise ValueError("Contact source redirected outside company website")
                found = {e.casefold() for e in EMAIL.findall(page.text)}
                if email.casefold() in found:
                    out.update({"Contact Verification": "public_email_found", "Contact Verified At": now_iso(),
                                "Contact Source URL": str(page.url)})
                else:
                    out["Contact Verification"] = "email_not_found"
            except Exception as exc:
                out["Contact Verification"] = "fetch_error"
                out["Contact Error"] = type(exc).__name__
    return out


def csv_text(rows, fields=None):
    fields = fields or list(dict.fromkeys(k for r in rows for k in r)) or ["Company", "Job Link"]
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def evidence_markdown(rows):
    lines = ["# Outreach evidence", "", "Fetched evidence is separate from prior research claims. No application or email was sent.", ""]
    for row in rows:
        lines += [f"## {row.get('Company', '')} — {row.get('Job Title', 'No verified job title')}", "",
                  f"- Status: {row['JD Status']}; checked {row['Checked At']}",
                  f"- Job: {row.get('Job Link', '')}", f"- Evidence: {row['JD Source URL']}",
                  f"- Fit: {row['Fit Status']}; {row['Fit Notes']}",
                  f"- Contact: {row['Contact Verification']}",
                  f"- Error: {row['Verification Error'] or 'none'}", "", row["JD Text"] or "No verified JD obtained.", ""]
    return "\n".join(lines)
