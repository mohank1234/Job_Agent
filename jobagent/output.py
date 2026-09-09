"""A bounded vacancy-first report, with evidence and no outreach side effects."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

from jobagent.matcher import rule_match
from jobagent.roles import classify
from jobagent.store import row_to_job
from jobagent.runtime import atomic_json, now_iso
from jobagent.outreach.service import recent
from jobagent.outreach.verification import (
    board_ref, canonical, csv_text, evidence_markdown, verify_row,
)

QA_TITLE = re.compile(r"\b(?:qa|sdet|quality|test(?:ing)?|tester|evaluation)\b", re.I)
FIELDS = [
    "Company", "Job Title", "Location", "Job Link", "JD Text", "Fit Status", "Workplace",
    "Fit Notes", "Profile Skills Mentioned", "Known Gaps Mentioned",
    "Experience Requirements (source excerpts)", "Requirements Excerpt (source)",
    "Salary (source)", "Listing Type",
    "JD Status", "JD Verified At", "JD Source URL", "JD SHA256", "Rule Score",
    "Scoring Method", "Checked At", "Verification Error",
]
FAILURES = {"blocked", "fetch_error", "description_missing", "unsupported_source", "ambiguous_posting"}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def read_leads(path):
    reader = csv.DictReader(io.StringIO(Path(path).read_text(encoding="utf-8-sig")))
    headers = reader.fieldnames or []
    if not {"Company", "Job Link"}.issubset(headers) or len(headers) != len(set(headers)):
        raise ValueError("Lead CSV needs unique Company and Job Link columns")
    rows = list(reader)
    if any(None in r or None in r.values() for r in rows):
        raise ValueError("Malformed lead CSV")
    return rows


def database_leads(path, profile, limit=50, max_age_days=21):
    """Read the existing DB without a migration or write; reassess cached fits.

    Cached text is used only to choose what to fetch. Verification replaces it.
    """
    if not Path(path).exists():
        return []
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    try:
        rows = conn.execute(
            "SELECT * FROM seen WHERE COALESCE(NULLIF(last_seen,''),first_seen)>=? "
            "AND (lower(title) LIKE '%qa%' OR lower(title) LIKE '%sdet%' "
            "OR lower(title) LIKE '%quality%' OR lower(title) LIKE '%test%' "
            "OR lower(title) LIKE '%evaluation%')", (cutoff,)).fetchall()
    finally:
        conn.close()
    candidates = []
    for row in rows:
        if not board_ref(row["url"]) or not QA_TITLE.search(row["title"]):
            continue
        job = row_to_job(dict(row))
        job.apply_classification(classify(job, profile))
        rule_match(job, profile)
        if not job.candidate or job.match_category not in ("A", "B", "C"):
            continue
        candidates.append((job.score, row["last_seen"] or row["first_seen"], {
            "Company": row["company"], "Job Title": row["title"], "Job Link": row["url"],
        }))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in candidates[:limit]]


def deduplicate(rows):
    seen, result = set(), []
    for row in rows:
        url = row.get("Job Link", "").strip()
        if not url:
            continue
        key = canonical(url)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def partition(rows):
    tabs = {"Ready to review": [], "Fit needs checking": [], "Excluded jobs": [], "Failed checks": []}
    for original in rows:
        row = dict(original)
        live = (row.get("JD Status") == "verified_live" and
                recent(row.get("JD Verified At"), max_age_days=1) and
                len(row.get("JD Text", "").strip()) >= 200 and
                row.get("Job Link", "").startswith("https://") and
                row.get("Job Title", "").strip() and
                hashlib.sha256(row["JD Text"].encode()).hexdigest() == row.get("JD SHA256"))
        if not live:
            if row.get("JD Status") == "verified_live":
                row["JD Status"] = "stale_or_invalid_evidence"
                row["Verification Error"] = "Refresh this posting before treating it as a current vacancy"
            target = "Failed checks"
        elif row.get("Fit Status") == "in_scope":
            target = "Ready to review"
        elif row.get("Fit Status") == "out_of_scope":
            target = "Excluded jobs"
        else:
            target = "Fit needs checking"
        tabs[target].append(row)
    for values in tabs.values():
        values.sort(key=lambda r: (-int(r.get("Rule Score") or 0), r.get("Company", ""), r.get("Job Title", "")))
    return tabs


def build_report(profile, leads, out, *, limit=50, discovery_queries=(), search=None, progress=None):
    from jobagent.startup_output import load_research, enrich_rows, write_outreach
    research = load_research(out)
    if not 1 <= limit <= 100:
        raise ValueError("Report limit must be between 1 and 100")
    if len(discovery_queries) > 3:
        raise ValueError("At most three discovery queries per refresh")
    issues, all_leads = [], list(leads)
    for query in discovery_queries:
        try:
            if search is None:
                from jobagent.enrich.exa import exa_search
                search = exa_search
            found = search(query, num_results=10, include_domains=[
                "jobs.ashbyhq.com", "jobs.lever.co", "job-boards.greenhouse.io"])
            new = [{"Company": board_ref(r["url"])[1], "Job Link": r["url"],
                    "Discovery Title (unverified)": r["title"]}
                   for r in found if board_ref(r["url"])]
            all_leads = new + all_leads
        except Exception as exc:
            issues.append({"stage": "discovery", "query": query, "error": type(exc).__name__})
    all_leads = deduplicate(all_leads)
    cache, verified = {}, []
    with httpx.Client(headers={"User-Agent": "JobAgent/1.0 (personal job search)"}) as client:
        for row in all_leads[:limit]:
            result = verify_row(row, profile, client, board_cache=cache)
            verified.append(result)
            if progress:
                progress(f"{result.get('Company')}: {result['JD Status']} / {result['Fit Status']}")
        # Current boards can contain fresh vacancies absent from yesterday's DB.
        # Expand only boards already fetched; no unbounded company crawl.
        seen = {canonical(r["Job Link"]) for r in verified}
        new_jobs = []
        for entry in cache.values():
            if isinstance(entry, Exception):
                continue
            for job in entry[0]:
                # Discovery may return intermediaries. Preserve those leads for
                # review, but do not let their catalogue consume the shortlist.
                if (board_ref(job.url) or (None, None))[1] == "jobgether":
                    continue
                if canonical(job.url) in seen or not QA_TITLE.search(job.title):
                    continue
                job.apply_classification(classify(job, profile))
                rule_match(job, profile)
                if job.candidate and job.match_category in ("A", "B", "C"):
                    new_jobs.append(job)
        for job in sorted(new_jobs, key=lambda j: -j.score):
            if len(verified) >= limit:
                break
            key = canonical(job.url)
            if key in seen:
                continue
            seen.add(key)
            result = verify_row({"Company": job.company, "Job Link": job.url}, profile, client, board_cache=cache)
            verified.append(result)
            if progress:
                progress(f"{result['Company']}: new board vacancy / {result['Fit Status']}")
    shortlist = enrich_rows(verified, research)
    tabs = partition(verified)
    summary = {
        "schema_version": 1, "checked_at": now_iso(), "checked": len(verified),
        "counts": {k: len(v) for k, v in tabs.items()}, "issues": issues,
        "lead_count": len(all_leads), "lead_limit": limit,
        "leads_not_checked": max(0, len(all_leads) - min(len(all_leads), limit)),
        "status": "partial" if issues or any(r["JD Status"] in FAILURES for r in verified) else "ok",
        "scope": "Bounded public ATS checks. Full descriptions are source text; fit is a rules-based assessment.",
        "applications": "No submissions observed by this workflow",
        "replies": "Not measured by this report; use outreach-status with Gmail read consent",
    }
    if not verified:
        summary["status"] = "partial"
        summary["issues"].append({"stage": "input", "error": "No vacancy links found"})
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "verified.csv").write_text(csv_text(verified), encoding="utf-8-sig")
    (out / "evidence.md").write_text(evidence_markdown(verified), encoding="utf-8")
    atomic_json(out / "verification.json", summary)
    extras = {"Startup shortlist": write_outreach(out, shortlist)} if research["roles"] else None
    summary["startup_shortlist_count"] = len(shortlist)
    atomic_json(out / "verification.json", summary)
    workbook = make_workbook(tabs, summary, extra_grids=extras)
    (out / "JobAgent Report.xlsx").write_bytes(workbook)
    return summary, verified


def text_cell(value):
    # Excel's cell limit is lower than Google Sheets'. Fail rather than silently
    # cutting a JD. Control characters forbidden by XML are made visible.
    value = "" if value is None else str(value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", value)
    if len(value) > 32767:
        raise ValueError("Cell exceeds Excel's 32767-character limit; full evidence stays in CSV/Markdown")
    return value


def make_workbook(tabs, summary, *, extra_grids=None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    wb.remove(wb.active)
    overview = [
        ["JobAgent Report", "Verified vacancies and source evidence"],
        ["Last checked (UTC)", summary["checked_at"]],
        ["Run status", summary["status"]],
        ["How to use", "Start with Ready to review. Fit needs checking contains specific gaps to resolve."],
        ["Refresh", "Generated report. Record actual applications in your tracker; edits here stop automatic replacement. A deleted optional tracker is not recreated."],
        ["Evidence", "Each current vacancy has a full JD, application link, source URL and verification time."],
        ["Fit", "Rules-based profile/JD comparison. A match does not confirm every requirement or employer eligibility."],
        ["Coverage", summary["scope"]],
        ["Vacancies checked", summary["checked"]],
        *[[name, len(rows)] for name, rows in tabs.items()],
        ["Additional leads outside run limit", summary["leads_not_checked"]],
        ["Applications", summary["applications"]], ["Replies", summary["replies"]],
        ["Discovery errors", json.dumps(summary["issues"], ensure_ascii=False)],
    ]
    data = {"Start here": overview}
    if extra_grids:
        overview.insert(4, ["Startup shortlist", "Investor-backed roles, public contact evidence and unsent tailored outreach. Research dates are separate from JD checks."])
        data.update(extra_grids)
    # Excel imports cap a cell at 32767 characters. Preserve unusually long
    # descriptions in adjacent continuation columns, with no truncation.
    longest = max((len(r.get("JD Text", "")) for rows in tabs.values() for r in rows), default=0)
    parts = max(1, (longest + 31999) // 32000)
    fields = list(FIELDS)
    extra = [f"JD Text (continued {i})" for i in range(2, parts + 1)]
    fields[fields.index("JD Text") + 1:fields.index("JD Text") + 1] = extra
    for name, rows in tabs.items():
        grid = [fields]
        for original in rows:
            row = dict(original)
            description = row.get("JD Text", "")
            row["JD Text"] = description[:32000]
            for i, field in enumerate(extra, 1):
                row[field] = description[i * 32000:(i + 1) * 32000]
            grid.append([row.get(f, "") for f in fields])
        data[name] = grid
    for name, grid in data.items():
        ws = wb.create_sheet(name)
        for row in grid:
            ws.append([text_cell(x) for x in row])
        # Explicit strings prevent formula injection from source-controlled text.
        for row in ws:
            for cell in row:
                cell.data_type = "s"
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        for cell in ws[1]:
            cell.fill = PatternFill("solid", fgColor="17365D")
            cell.font = Font(color="FFFFFF", bold=True)
        ws.freeze_panes = "C2" if name != "Start here" else "A2"
        if name != "Start here":
            ws.auto_filter.ref = ws.dimensions
            for i in range(2, ws.max_row + 1):
                ws.row_dimensions[i].height = 80
            for index, field in enumerate(grid[0], 1):
                widths = {"Company": 20, "Job Title": 40, "Location": 24, "Job Link": 38,
                          "JD Text": 85, "Fit Notes": 55, "Profile Skills Mentioned": 45, "Workplace": 14,
                          "Cold Email": 85, "LinkedIn Note": 55, "Requirements To Confirm": 60,
                          "Email Evidence": 60, "Why This Role": 65}
                ws.column_dimensions[get_column_letter(index)].width = 85 if field.startswith("JD Text") else widths.get(field, 27)
        else:
            ws.column_dimensions["A"].width = 36
            ws.column_dimensions["B"].width = 110
            for i in range(2, ws.max_row + 1):
                ws.row_dimensions[i].height = 32
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def workbook_digest(data):
    """Compare cell values after Google's XLSX conversion, not ZIP metadata."""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    values = []
    try:
        for ws in wb:
            grid = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                grid.append(cells)
            while grid and not grid[-1]:
                grid.pop()
            values.append([ws.title, grid])
    finally:
        wb.close()
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
