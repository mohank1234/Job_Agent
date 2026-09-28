"""Founder / CEO / CTO contacts for small startups (about 10-200 people).

Sources, all public and all read through search or documented APIs:
- hiring contacts named on the company's Y Combinator job posting;
- the leader already found by company research;
- search-engine (Exa) copies of public LinkedIn profile pages, whose text
  starts "# Name" followed by a headline such as "Co-Founder & CTO at X".
  LinkedIn itself is never fetched or scraped.
Employee count comes from the search-engine copy of the company's LinkedIn
page ("Employees: 52", "Company Size: 11-50 employees").

Emails come only from a mailbox verification service (jobagent.enrich.
email_finder): an address is used only when verified deliverable and safe to
send, on the company's own domain. Nothing is guessed and sent unverified.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from jobagent.runtime import now_iso

LEADER = re.compile(r"\b(?:co-?\s?founder|founder|ceo|chief executive|cto|chief technology)\b", re.I)
EXACT = re.compile(r"Employees:\s*([\d,]+)|employs\s+([\d,]+)\s+people", re.I)
BAND = re.compile(r"Company Size:\s*([\d,]+)\s*[-–]\s*([\d,]+)\s+employees", re.I)
NOT_COMPANY_SITES = ("linkedin.", "ycombinator.", "workatastartup.", "crunchbase.", "ashbyhq.", "lever.co",
                     "greenhouse.io", "github.", "twitter.", "x.com", "medium.", "wikipedia.", "youtube.",
                     "facebook.", "instagram.", "glassdoor.", "indeed.", "wellfound.", "pitchbook.",
                     "tracxn.", "producthunt.", "techcrunch.", "prnewswire.", "businesswire.", "prweb.")


def key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


def plain_name(company):
    """Careers-board slugs such as "bidgely-inc" as a searchable name ("bidgely")."""
    words = re.split(r"[-_\s]+", (company or "").strip())
    while len(words) > 1 and words[-1].casefold() in ("inc", "llc", "ltd", "hq", "corp", "co", "gmbh", "pvt", "limited"):
        words.pop()
    return " ".join(words)


def _num(text):
    return int(text.replace(",", ""))


def parse_size(text):
    """(low, high) employees from search-indexed LinkedIn company text."""
    exact = EXACT.search(text or "")
    if exact:
        n = _num(exact.group(1) or exact.group(2))
        return n, n
    band = BAND.search(text or "")
    if band:
        return _num(band.group(1)), _num(band.group(2))
    return None


def size_label(size):
    return "" if not size else (str(size[0]) if size[0] == size[1] else f"{size[0]}-{size[1]}")


def in_target(size, low=10, high=200):
    """Exact counts must fall in low..high; a LinkedIn band must sit inside it
    (11-50 and 51-200 qualify, 2-10 and 201-500 do not)."""
    return bool(size) and size[0] >= low and size[1] <= high


def company_size(company, search):
    """{'Employee Count', 'Employee Count Source'} or {} when not found."""
    company = plain_name(company)
    wanted = key(company)
    for result in search(f"{company} company LinkedIn employees", 5, include_domains=["linkedin.com"],
                         max_characters=2500):
        url, text = result.get("url", ""), result.get("snippet", "")
        if "/company/" not in url:
            continue
        heading = next((line[2:] for line in text.splitlines() if line.startswith("# ")), result.get("title", ""))
        slug = urlsplit(url).path.rstrip("/").split("/")[-1]
        if wanted and (wanted == key(slug) or key(heading).startswith(wanted)):
            size = parse_size(text)
            if size:
                return {"Employee Count": size_label(size), "Employee Count Source": url}
    return {}


def profile_leaders(company, search):
    """Founders/CEO/CTO whose public profile headline names this company."""
    company = plain_name(company)
    wanted, found = key(company), []
    for result in search(f"{company} founder CEO CTO", 8, include_domains=["linkedin.com"], max_characters=600):
        url = result.get("url", "")
        if not re.search(r"linkedin\.com/in/[A-Za-z0-9_%.-]+/?$", url):
            continue
        lines = [line.strip() for line in result.get("snippet", "").splitlines() if line.strip()]
        name = lines[0][2:].strip() if lines and lines[0].startswith("# ") else result.get("title", "").split(" - ")[0]
        headline = lines[1] if len(lines) > 1 else ""
        if name and LEADER.search(headline) and wanted and wanted in key(headline):
            found.append({"name": name, "title": headline[:160], "url": url, "source": url})
    return found


def rank(title):
    title = title or ""
    if re.search(r"\bcto\b|chief technology", title, re.I):
        return 0
    if re.search(r"founder", title, re.I):
        return 1
    if re.search(r"\bceo\b|chief executive", title, re.I):
        return 2
    return 3


def leaders(row, meta, search=None):
    """Up to three leaders, CTO first, then founders, then CEO."""
    people = []
    try:
        import json
        contacts = json.loads(row.get("Hiring Contacts") or "[]")
    except ValueError:
        contacts = []
    for c in contacts:
        # A YC posting's hiring contact is usually a founder even when untitled.
        if c.get("name") and (not c.get("title") or LEADER.search(c["title"])):
            people.append({"name": c["name"], "title": c.get("title") or "Hiring contact on YC posting",
                           "url": c.get("url") or row.get("Job Link", ""), "source": row.get("Job Link", "")})
    if meta.get("Manager Name") and LEADER.search(meta.get("Manager Role", "")):
        people.append({"name": meta["Manager Name"], "title": meta["Manager Role"],
                       "url": meta.get("Manager LinkedIn") or meta.get("Manager Source", ""),
                       "source": meta.get("Manager Source", "")})
    if search is not None:
        try:
            people += profile_leaders(row["Company"], search)
        except Exception:
            pass
    unique = {}
    for p in people:
        unique.setdefault(key(p["name"]), p)
    return sorted(unique.values(), key=lambda p: rank(p["title"]))[:3]


def company_domain(company, urls):
    """The company's own web domain, from URLs whose host carries its name."""
    brand = key(plain_name(company))
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if not host or any(site in host for site in NOT_COMPANY_SITES):
            continue
        labels = host.split(".")
        root = ".".join(labels[-3:] if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac") else labels[-2:])
        if brand and (brand in key(root) or key(root.split(".")[0]) in brand) and len(key(root.split(".")[0])) >= 3:
            return root
    return ""


def split_name(name):
    parts = [p for p in re.split(r"\s+", re.sub(r"\(.*?\)|,.*$", "", name or "").strip()) if p]
    return (parts[0], parts[-1]) if len(parts) >= 2 else None


def verified_contact(people, domain, finder):
    """The best-ranked leader with a verified address on the company domain."""
    wanted = []
    for p in people:
        parts = split_name(p["name"])
        if parts:
            wanted.append((p, {"firstName": parts[0], "surname": parts[1], "domain": domain}))
    if not wanted or not domain:
        return {}
    # One person per lookup, best-ranked first, stopping at the first verified
    # address: each tested address pattern is charged, so this is cheapest.
    for person, query in wanted:
        found = finder([query])
        item = found.get((query["firstName"].casefold(), query["surname"].casefold()))
        email = (item or {}).get("email", "")
        if not email or not email.lower().endswith("@" + domain.lower()):
            continue
        source = person["source"] if (person.get("source") or "").startswith("http") else person.get("url", "")
        if not source.startswith("http"):
            continue
        return {
            "Public Work Email": email, "Email Source": source,
            "Email Contact Name": person["name"], "Email Contact Role": person["title"],
            "Email Contact LinkedIn": person["url"] if "linkedin.com/in/" in person.get("url", "") else "",
            "Email Evidence": (f"Mailbox verified deliverable and safe to send by an email verification service "
                               f"({item.get('validationStatus') or 'valid'}, score {item.get('overallScore', 'n/a')}); "
                               f"name and role from {source}"),
            "Email Ownership Status": "Verified deliverable mailbox; address not published by the person",
            "Email Ownership Checked At": now_iso(),
        }
    return {}
