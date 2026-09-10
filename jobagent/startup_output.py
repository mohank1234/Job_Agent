"""Publish researched startup leads and evidence-bound outreach drafts.

Research is a dated, human-reviewed input. Refreshing a JD never silently
refreshes a person's affiliation, email evidence, or the contents of a draft.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jobagent.outreach.service import recent
from jobagent.outreach.verification import canonical, csv_text

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
    "Email Template",
]
SHORTLIST_FIELDS = ["Company", "Job Title", "Location", "Job Link", "JD Text",
                    "Fit Status", "Fit Notes", "Framework Review", "Duplicate Listing URLs", *RESEARCH_FIELDS,
                    "JD Verified At", "JD Source URL", "JD SHA256"]


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
        row["Research Status"] = "Dated public research; email delivery not tested" if fresh_research else "Research older than 30 days; recheck contacts and backing"
        jd = row.get("JD Text", "")
        live = (row.get("JD Status") == "verified_live" and recent(row.get("JD Verified At"), max_age_days=1)
                and len(jd.strip()) >= 200 and hashlib.sha256(jd.encode()).hexdigest() == row.get("JD SHA256"))
        unchanged = row.get("JD SHA256") == entry.get("Draft JD SHA256")
        if not live or not unchanged or not fresh_research:
            row["Draft Status"] = "Withheld: refresh vacancy/research and review draft against current JD"
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
    export = [{key: row.get(key, "") for key in SHORTLIST_FIELDS} for row in shortlist]
    (out / "Startup Outreach.csv").write_text(csv_text(export, SHORTLIST_FIELDS), encoding="utf-8-sig")
    lines = ["# Startup jobs and outreach drafts", "",
             "Vacancies are checked against live employer ATS boards. Investor and contact research has its own date.",
             "Public contact records do not prove email delivery or that a senior leader is the hiring manager.",
             "These are unsent drafts. Third-party and historical email records require a fresh check before use.", ""]
    for row in shortlist:
        lines += [f"## {row['Company']} — {row['Job Title']}", "",
                  f"Apply: {row['Job Link']}", f"JD checked: {row.get('JD Verified At', '')}",
                  f"Full JD: {row.get('JD Source URL', '')}", ""]
        for key in RESEARCH_FIELDS:
            lines += [f"**{key}:** {row.get(key) or 'Not publicly found / not supplied'}", ""]
    (out / "Startup Outreach.md").write_text("\n".join(lines), encoding="utf-8")
    return [SHORTLIST_FIELDS, *[[r.get(f, "") for f in SHORTLIST_FIELDS] for r in shortlist]]
