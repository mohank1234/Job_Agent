"""Does a startup have a relevant QA opening right now? (free, public sources)

Checked in order, stopping at the first source that answers:
1. jobs already verified today from the company's careers board;
2. its public ATS board (Ashby, Greenhouse, Lever) found from its name or YC
   slug, read through the documented board APIs;
3. its own website's careers pages, whose links to an ATS board are then read
   the same way.
Only an ATS posting with a full job description counts as an opening. A
company with no findable board is reported as checked with no public QA
opening; that is not a claim about unpublished hiring needs.
"""
from __future__ import annotations

import re

import httpx

from jobagent.runtime import now_iso

CAREERS_PATHS = ("", "/careers", "/jobs")


def _key(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").casefold())


def _relevant(rows):
    return [r for r in rows if r.get("Fit Status") != "out_of_scope"]


def check_openings(company, profile, *, existing_rows=(), existing_coverage=(), client=None,
                   probe=None, fetch=None, get=None):
    from jobagent import discovery
    from jobagent.morning import QA_TITLE, _job_dict, assess_boards
    from jobagent.outreach.verification import board_ref, fetch_board, get_public
    probe = probe or discovery.probe_board
    fetch = fetch or fetch_board
    get = get or get_public
    names = {_key(company["name"]), _key(company.get("slug"))} - {""}
    result = {"company": company, "checked_at": now_iso(), "rows": [], "coverage": [], "sources": [],
              "has_relevant_opening": False, "complete": False, "status": ""}

    # 1. Already verified today by the main careers-board scan.
    own = [r for r in existing_rows if _key(r.get("Company")) in names]
    covered = [c for c in existing_coverage if _key(c.get("Company")) in names and c.get("Status") == "Fetched"]
    if own or covered:
        relevant = _relevant(own)
        result.update(rows=relevant, has_relevant_opening=bool(relevant), complete=True,
                      sources=[c.get("Board URL", "") for c in covered],
                      status=(f"{len(relevant)} relevant QA opening(s) on its careers board" if relevant
                              else "Careers board checked today; no relevant QA opening"))
        return result

    owned = client is None
    client = client or httpx.Client(timeout=25, headers={"User-Agent": "JobAgent/1.0 (personal job search)"})
    try:
        # 2. Its ATS board, from its name or YC slug.
        board = probe(company["name"], client) or (probe(company["slug"], client) if company.get("slug") else None)
        # 3. Links to an ATS board on its own careers pages.
        pages_read, mentions = 0, []
        if not board and company.get("website"):
            base = company["website"].rstrip("/")
            for path in CAREERS_PATHS:
                try:
                    html = get(client, base + path).text
                except Exception:
                    continue
                pages_read += 1
                result["sources"].append(base + path)
                links = re.findall(r'href=["\'](https?://[^"\']+)["\']', html)
                board = next((u for u in links if board_ref(u)), None)
                if board:
                    break
                text = re.sub(r"<[^>]+>", " ", html)
                mentions += [m.group(0).strip() for m in re.finditer(
                    r"\b(?:senior\s+)?(?:qa|sdet|quality\s+(?:assurance|engineer)|test\s+automation)[\w /-]{0,30}(?:engineer|analyst|lead)\b",
                    text, re.I)][:3]
        if not board:
            mention = (f"; careers page mentions {mentions[0]!r} (not on a job board, review before sending)"
                       if mentions else "")
            result.update(complete=pages_read > 0,
                          status=("No public ATS careers board found on its website" + mention if pages_read
                                  else "Website unreachable; openings not checked"))
            return result
        ref = board_ref(board)
        url = {"ashby": "https://jobs.ashbyhq.com/{}", "lever": "https://jobs.lever.co/{}",
               "greenhouse": "https://job-boards.greenhouse.io/{}"}[ref[0]].format(ref[1])
        result["sources"].append(url)
        try:
            jobs, source = fetch(url, client)
        except Exception as exc:
            result.update(status=f"Careers board {url} could not be read ({type(exc).__name__})")
            return result
        qa = [j for j in jobs or [] if QA_TITLE.search(j.title)]
        record = {"Company": ref[1], "Vendor": ref[0], "Board URL": url, "Checked At": now_iso(), "Status": "Fetched",
                  "Source URL": source, "Postings": len(jobs or []), "QA titles": len(qa),
                  "jobs": [_job_dict(j) for j in qa]}
        rows = _relevant(assess_boards([record], profile))
        result.update(rows=rows, coverage=[record], has_relevant_opening=bool(rows), complete=True,
                      status=(f"{len(rows)} relevant QA opening(s) on {url}" if rows
                              else f"No relevant QA opening on {url} ({len(jobs or [])} postings checked)"))
        return result
    finally:
        if owned:
            client.close()
