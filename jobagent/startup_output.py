"""Publish researched startup leads and evidence-bound outreach drafts.

Research is a dated, human-reviewed input. Refreshing a JD never silently
refreshes a person's affiliation, email evidence, or the contents of a draft.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from jobagent.outreach.service import recent
from jobagent.outreach.verification import canonical, csv_text
from jobagent.runtime import now_iso

TRACKER_FIELDS = [
    "Domain", "Website", "Startup Source", "Team Size", "Industry", "Region", "Remote Status",
    "Has Relevant Opening", "Opening Checked At", "Opening Check Status", "Job Source",
    "Date Discovered", "Email Provider", "Email Verification Status", "Email Status",
    "Date Created", "Date Contacted", "Follow-up Status", "Duplicate Key", "Notes",
]

RESEARCH_FIELDS = [
    "Startup Priority", "Investor Backing", "YC Batch", "Investment Source",
    "Research Checked At", "Research Status", "Manager Name", "Manager Role",
    "Manager LinkedIn", "Manager Source", "Public Work Email", "Email Evidence",
    "Email Source", "Other Public Contact", "Other Contact Evidence",
    "Why This Role", "Requirements To Confirm", "Draft Status", "Cold Email Subject",
    "Cold Email", "LinkedIn Note", "LinkedIn Note Characters",
    "Approval Status", "Draft Generation", "Contact Status", "Backing Status",
    "Email Ownership Status", "Email Ownership Checked At",
    "Email Contact Name", "Email Contact Role", "Email Contact LinkedIn", "Company Display Name",
    "Email Template", "Employee Count", "Employee Count Source",
    "Resume File", "ATS Match", "ATS Matched Keywords", "ATS Missing Keywords", "Resume Headline",
    *TRACKER_FIELDS,
]
SHORTLIST_FIELDS = ["Company", "Job Title", "Location", "Job Link", "JD Text",
                    "Fit Status", "Fit Notes", "Framework Review", "Duplicate Listing URLs", *RESEARCH_FIELDS,
                    "JD Verified At", "JD Source URL", "JD SHA256"]


def opening_flag(row):
    """None means an older job row, which keeps its existing review rules."""
    value = row.get("Has Relevant Opening")
    if value is True or str(value).casefold() in ("true", "yes", "1"):
        return True
    if value is False or str(value).casefold() in ("false", "no", "0"):
        return False
    return None


def outreach_domain(row):
    value = row.get("Domain") or row.get("Website") or ""
    try:
        host = urlsplit(value if "://" in value else "https://" + value).hostname or ""
    except ValueError:
        return ""
    return host.casefold().removeprefix("www.")


def duplicate_key(row):
    """Stable audit identity; delivery also enforces one approach per company."""
    company = outreach_domain(row) or re.sub(r"[^a-z0-9]", "", row.get("Company", "").casefold())
    email = (row.get("Public Work Email") or "").strip().casefold()
    contact = email or (row.get("Email Contact Name") or row.get("Manager Name") or "").casefold()
    role = "proactive" if opening_flag(row) is False else canonical(row.get("Job Link", "")) or row.get("Job Title", "").casefold()
    return hashlib.sha256("|".join((company, contact, role)).encode()).hexdigest()[:24]


def tracker_row(row, state=None):
    """Fill report aliases without manufacturing contacts or opening evidence."""
    row = dict(row)
    state = state or {}
    row["Domain"] = outreach_domain(row)
    for target, sources in {
        "Team Size": ("Employee Count",), "Region": ("Location",),
        "Date Discovered": ("Research Checked At", "JD Verified At"),
        "Date Created": ("Research Checked At",),
        "Email Verification Status": ("Email Ownership Status",),
    }.items():
        if not row.get(target):
            row[target] = next((row.get(source) for source in sources if row.get(source)), "")
    row["Date Created"] = state.get("Date Created") or row.get("Date Created") or now_iso()
    row["Date Contacted"] = state.get("Sent At") or row.get("Date Contacted") or ""
    row["Follow-up Status"] = state.get("Follow-up Status") or row.get("Follow-up Status") or "Not scheduled"
    row["Duplicate Key"] = duplicate_key(row)
    row["Email Status"] = state.get("Status") or row.get("Email Status") or row.get("Draft Status") or "Pending research"
    if state.get("Resume Attachment"):
        row["Resume File"] = row.get("Resume File") or state["Resume Attachment"]
    if not row.get("Email Provider"):
        source = (row.get("Email Source") or "").casefold()
        # Codes: COMPANY_WEBSITE / PROSPEO / HUNTER / TOMBA / NO_VERIFIED_EMAIL;
        # PUBLIC_SOURCE for an address from research or a public hiring post.
        row["Email Provider"] = ("HUNTER" if "hunter.io" in source else "PUBLIC_SOURCE" if source
                                 else "NO_VERIFIED_EMAIL" if row.get("Company") else "")
    return row


def load_research(out):
    path = Path(out) / "startup-research.json"
    if not path.exists():
        return {"version": 1, "roles": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("roles"), list):
        raise ValueError("Invalid startup research catalog")
    seen = set()
    for role in data["roles"]:
        key = canonical(role["Job Link"])
        if key in seen:
            raise ValueError("Duplicate startup research job")
        seen.add(key)
        if len(role.get("LinkedIn Note", "")) > 300:
            raise ValueError("LinkedIn note exceeds 300 characters")
        if role.get("Public Work Email") and not (role.get("Email Source") and role.get("Email Evidence")):
            raise ValueError("Public work email requires source and evidence classification")
        if (role.get("Investor Backing") and not role.get("Investment Source")) or (role.get("Manager Name") and not role.get("Manager Source")):
            raise ValueError("Startup and manager claims require source links")
    return data


def research_leads(out):
    return [{"Company": r["Company"], "Job Link": r["Job Link"]}
            for r in load_research(out)["roles"]]


def unique_notes(*values):
    seen, result = set(), []
    for value in values:
        for note in value.split(';'):
            note = note.strip()
            if note and note.casefold() not in seen:
                seen.add(note.casefold())
                result.append(note)
    return '; '.join(result)


def enrich_rows(rows, research):
    lookup = {canonical(r["Job Link"]): r for r in research["roles"]}
    shortlist = []
    for row in rows:
        row['Fit Notes'] = unique_notes(row.get('Fit Notes', ''))
        # Previous exports are also discovery inputs. Do not retain stale drafts.
        for field in RESEARCH_FIELDS:
            row.pop(field, None)
        entry = lookup.get(canonical(row.get("Job Link", "")))
        if not entry:
            continue
        row.update({key: entry.get(key, "") for key in RESEARCH_FIELDS})
        fresh_research = recent(entry.get("Research Checked At"), max_age_days=30)
        verified = (entry.get("Email Ownership Status") or "").startswith("Verified deliverable")
        row["Research Status"] = ("Research older than 30 days; recheck contacts and backing" if not fresh_research
                                  else "Dated public research; email mailbox verified deliverable" if verified
                                  else "Dated public research; email delivery not tested")
        jd = row.get("JD Text", "")
        live = (row.get("JD Status") == "verified_live" and recent(row.get("JD Verified At"), max_age_days=1)
                and len(jd.strip()) >= 200 and hashlib.sha256(jd.encode()).hexdigest() == row.get("JD SHA256"))
        unchanged = row.get("JD SHA256") == entry.get("Draft JD SHA256")
        opening = opening_flag(entry)
        startup_contact_ready = True
        if opening is not None:
            from jobagent.outreach.founders import verified_leadership_contact
            startup_contact_ready = (verified_leadership_contact(row, outreach_domain(row))
                                     and recent(row.get("Email Ownership Checked At"), max_age_days=30))
        if opening is False:
            checked = (recent(row.get("Opening Checked At"), max_age_days=1)
                       and str(row.get("Opening Check Status", "")).casefold().startswith("checked"))
            if not fresh_research or not checked or not startup_contact_ready:
                row["Draft Status"] = "Withheld: proactive enquiry requires fresh startup research, opening check and verified leadership email"
                for field in ("Cold Email Subject", "Cold Email", "LinkedIn Note"):
                    row[field] = ""
            else:
                row["Draft Status"] = "Draft only; proactive enquiry, no public QA opening found"
        elif not live or not unchanged or not fresh_research:
            row["Draft Status"] = "Withheld: refresh vacancy/research and review draft against current JD"
            for key in ("Cold Email Subject", "Cold Email", "LinkedIn Note"):
                row[key] = ""
        elif not startup_contact_ready:
            row["Draft Status"] = "Withheld: verified professional leadership email required"
            for key in ("Cold Email Subject", "Cold Email", "LinkedIn Note"):
                row[key] = ""
        elif row.get("Fit Status") == "out_of_scope":
            row["Draft Status"] = "Withheld: role outside profile scope"
            for key in ("Cold Email Subject", "Cold Email", "LinkedIn Note"):
                row[key] = ""
        else:
            row["Draft Status"] = "Draft only; review requirements and recipient evidence before use"
            if entry.get("Requires Fit Review") and row.get("Fit Status") == "in_scope":
                row["Fit Status"] = "needs_review"
            if entry.get("Requirements To Confirm"):
                row["Fit Notes"] = unique_notes(row.get('Fit Notes', ''), entry['Requirements To Confirm'])
        row["LinkedIn Note Characters"] = str(len(row.get("LinkedIn Note", "")))
        shortlist.append(row)
    order = {canonical(r["Job Link"]): n for n, r in enumerate(research["roles"])}
    shortlist.sort(key=lambda r: order[canonical(r["Job Link"])])
    return shortlist


def write_outreach(out, shortlist):
    out = Path(out)
    states_path = out / "Gmail Draft Status.json"
    states = json.loads(states_path.read_text(encoding="utf-8")).get("drafts", []) if states_path.exists() else []
    lookup = {canonical(s["Job Link"]): s for s in states if s.get("Job Link")}
    shortlist = [tracker_row(row, lookup.get(canonical(row.get("Job Link", "")))) for row in shortlist]
    export = [{key: row.get(key, "") for key in SHORTLIST_FIELDS} for row in shortlist]
    (out / "Startup Outreach.csv").write_text(csv_text(export, SHORTLIST_FIELDS), encoding="utf-8-sig")
    lines = ["# Startup jobs and outreach drafts", "",
             "Opening checks distinguish live vacancies from proactive enquiries. Investor and contact research has its own date.",
             "Public contact records do not prove email delivery or that a senior leader is the hiring manager.",
             "These are unsent drafts. Third-party and historical email records require a fresh check before use.", ""]
    for row in shortlist:
        lines += [f"## {row['Company']} — {row.get('Job Title', '')}", "",
                  f"{'Startup' if opening_flag(row) is False else 'Apply'}: {row.get('Job Link', '')}", f"JD checked: {row.get('JD Verified At', '')}",
                  f"Full JD: {row.get('JD Source URL', '')}", ""]
        for key in RESEARCH_FIELDS:
            value = row.get(key)
            lines += [f"**{key}:** {value if value is False or value else 'Not publicly found / not supplied'}", ""]
    (out / "Startup Outreach.md").write_text("\n".join(lines), encoding="utf-8")
    return [SHORTLIST_FIELDS, *[[r.get(f, "") for f in SHORTLIST_FIELDS] for r in shortlist]]
