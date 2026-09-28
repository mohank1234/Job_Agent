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
from datetime import datetime, timezone
from urllib.parse import urlsplit

from jobagent.runtime import now_iso

LEADER = re.compile(r"\b(?:co[-\s]?founder|founder|ceo|chief executive|cto|chief technolog(?:y|ical))\b", re.I)
EXACT = re.compile(r"Employees:\s*([\d,]+)|employs\s+([\d,]+)\s+people", re.I)
BAND = re.compile(r"Company Size:\s*([\d,]+)\s*[-–]\s*([\d,]+)\s+employees", re.I)
NOT_COMPANY_SITES = ("linkedin.", "ycombinator.", "workatastartup.", "crunchbase.", "ashbyhq.", "lever.co",
                     "greenhouse.io", "github.", "twitter.", "x.com", "medium.", "wikipedia.", "youtube.",
                     "facebook.", "instagram.", "glassdoor.", "indeed.", "wellfound.", "pitchbook.",
                     "tracxn.", "producthunt.", "techcrunch.", "prnewswire.", "businesswire.", "prweb.")
PERSONAL_DOMAINS = frozenset(("gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "ymail.com",
                            "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com",
                            "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me", "mail.com",
                            "gmx.com", "gmx.net", "rediffmail.com", "zoho.com"))


def professional_domain(value):
    """Normalize an explicit company domain/website, excluding public platforms.

    This does not infer a website from a company name. Use company_domain for
    untrusted search results and this helper for source-provided websites.
    """
    value = str(value or "").strip()
    if not value or re.search(r"\s|@", value):
        return ""
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
        return ""
    host = host.removeprefix("www.")
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host):
        return ""
    if host in PERSONAL_DOMAINS or any(host.endswith("." + domain) for domain in PERSONAL_DOMAINS):
        return ""
    if any(site in host for site in NOT_COMPANY_SITES):
        return ""
    return host


def professional_email(email, domain):
    """Only a named work mailbox on the exact known company domain."""
    domain = professional_domain(domain)
    if not domain or not isinstance(email, str):
        return False
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email):
        return False
    local, host = email.rsplit("@", 1)
    if local.casefold() in {"privacy", "security", "support", "noreply", "no-reply", "marketing", "sales",
                           "info", "hello", "contact", "careers", "jobs", "recruiting", "hr"}:
        return False
    return host.casefold() == domain


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
        if name and rank(headline) < 4 and wanted and wanted in key(headline):
            found.append({"name": name, "title": headline[:160], "url": url, "source": url})
    return found


ENGINEERING_HEAD = re.compile(r"\b(?:vp|vice president|head)\b[^|,]{0,25}\bengineering\b", re.I)
SMALL_TEAM = 50


def rank(title, team_size=None):
    """Who to contact first. Up to 50 people: Founder/Co-founder, CTO, CEO.
    Larger (or unknown): CTO, technical co-founder, Founder/CEO. VP or Head of
    Engineering only when none of those is available."""
    title = title or ""
    cto = bool(re.search(r"\bcto\b|chief technolog(?:y|ical)", title, re.I))
    founder = bool(re.search(r"\bfounder\b|\bco[-\s]?founder\b", title, re.I))
    ceo = bool(re.search(r"\bceo\b|chief executive", title, re.I))
    if team_size is not None and team_size <= SMALL_TEAM:
        order = [founder, cto, ceo]
    else:
        order = [cto, founder and bool(re.search(r"technical|cto|engineering", title, re.I)), founder or ceo]
    for n, hit in enumerate(order):
        if hit:
            return n
    return 3 if ENGINEERING_HEAD.search(title) else 4


def leaders(row, meta, search=None, team_size=None):
    """Up to three leaders, in the contact order for the company's size."""
    people = []
    try:
        import json
        contacts = json.loads(row.get("Hiring Contacts") or "[]")
    except ValueError:
        contacts = []
    for c in contacts if isinstance(contacts, list) else []:
        # A posting's hiring contact may be a recruiter. Require a named role.
        if isinstance(c, dict) and c.get("name") and rank(c.get("title")) < 4:
            people.append({"name": c["name"], "title": c["title"],
                           "url": c.get("url") or row.get("Job Link", ""), "source": row.get("Job Link", "")})
    if meta.get("Manager Name") and rank(meta.get("Manager Role", "")) < 4:
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
        name_key = key(p["name"])
        if name_key not in unique or rank(p["title"], team_size) < rank(unique[name_key]["title"], team_size):
            unique[name_key] = p
    return sorted(unique.values(), key=lambda p: rank(p["title"], team_size))[:3]


def company_domain(company, urls):
    """The company's own web domain, from URLs whose host carries its name."""
    brand = key(plain_name(company))
    for url in urls:
        host = professional_domain(url)
        if not host:
            continue
        labels = host.split(".")
        root = ".".join(labels[-3:] if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac") else labels[-2:])
        if brand and (brand in key(root) or key(root.split(".")[0]) in brand) and len(key(root.split(".")[0])) >= 3:
            return root
    return ""


def split_name(name):
    parts = [p for p in re.split(r"\s+", re.sub(r"\(.*?\)|,.*$", "", name or "").strip()) if p]
    return (parts[0], parts[-1]) if len(parts) >= 2 else None


def contact_from_executives(company, executives, *, domain_given, team_size=None, expected_domain="", provider="Hunter"):
    """The best verified founder/CTO/CEO (VP or Head of Engineering only when
    none of them is listed) from a Hunter Domain Search or Apollo, in the
    contact order for the company's size. Searched by name only, the listed
    organisation must match the company."""
    wanted = key(plain_name(company))
    best = None
    for person in executives:
        domain = professional_domain(person.get("domain", ""))
        if expected_domain and domain != professional_domain(expected_domain):
            continue
        organization = key(person.get("organization", ""))
        if not domain_given and not (len(wanted) >= 3 and organization
                                     and (organization.startswith(wanted) or wanted.startswith(organization))):
            continue
        title = person.get("title", "")
        if not (person.get("verified") and person.get("name") and rank(title, team_size) < 4
                and professional_email(person.get("email", ""), domain)):
            continue
        if best is None or rank(title, team_size) < rank(best["title"], team_size):
            best = person
    if not best:
        return {}
    linkedin = best.get("linkedin") or ""
    linkedin = linkedin if linkedin.startswith("http") else ""
    source = best.get("source") or linkedin or f"https://{best['domain']}"
    if not source.startswith(("http://", "https://")):
        source = f"https://{best['domain']}"
    provider = best.get("provider") or provider
    return {
        "Public Work Email": best["email"], "Email Source": source,
        "Email Contact Name": best["name"], "Email Contact Role": best["title"],
        "Email Contact LinkedIn": linkedin if "linkedin.com/in/" in linkedin else "",
        "Email Provider": provider, "Email Verification Status": "verified", "Company Domain": professional_domain(best['domain']),
        "Email Evidence": (f"{provider}: verified deliverable ({best.get('organization') or best['domain']}, "
                           f"confidence {best.get('confidence', 'n/a')}); first public source {source}"),
        "Email Ownership Status": f"Verified deliverable mailbox; listed by {provider}",
        "Email Ownership Checked At": now_iso(),
    }


def verified_contact(people, domain, finder):
    """The best-ranked leader with a verified address on the company domain."""
    wanted = []
    for p in people:
        parts = split_name(p["name"])
        if parts and rank(p.get("title")) < 4:
            wanted.append((p, {"firstName": parts[0], "surname": parts[1], "domain": domain}))
    if not wanted or not domain:
        return {}
    # One person per lookup, best-ranked first, stopping at the first verified
    # address: each tested address pattern is charged, so this is cheapest.
    for person, query in wanted:
        found = finder([query])
        item = found.get((query["firstName"].casefold(), query["surname"].casefold()))
        email = (item or {}).get("email", "")
        status = str((item or {}).get("validationStatus") or "").casefold()
        safe = (status in ("valid", "verified", "hunter verified: valid") or
                ((item or {}).get("isDeliverable") is True and (item or {}).get("isSafeToSend") is True
                 and not any((item or {}).get(flag) for flag in ("isCatchAll", "isRoleAccount", "isDisposable"))))
        if not safe or not professional_email(email, domain):
            continue
        source = person["source"] if (person.get("source") or "").startswith("http") else person.get("url", "")
        if not source.startswith("http"):
            continue
        return {
            "Public Work Email": email, "Email Source": source,
            "Email Contact Name": person["name"], "Email Contact Role": person["title"],
            "Email Contact LinkedIn": person["url"] if "linkedin.com/in/" in person.get("url", "") else "",
            "Email Provider": "Hunter" if status.startswith("hunter") else "Email verification service",
            "Email Verification Status": "verified", "Company Domain": professional_domain(domain),
            "Email Evidence": (f"Mailbox verified deliverable and safe to send by an email verification service "
                               f"({item.get('validationStatus') or 'valid'}, score {item.get('overallScore', 'n/a')}); "
                               f"name and role from {source}"),
            "Email Ownership Status": "Verified deliverable mailbox; address not published by the person",
            "Email Ownership Checked At": now_iso(),
        }
    return {}


def verified_leadership_contact(meta, domain=""):
    """Whether cached evidence supports a verified company leadership address.

    The caller controls maximum evidence age; missing, invalid and future
    timestamps are rejected here. Publicly sourced alone is not verified.
    """
    domain = domain or meta.get("Company Domain") or meta.get("Domain") or ""
    email = meta.get("Public Work Email") or ""
    # Older adapter records predate Company Domain but have an explicit
    # verification status and were checked against the company on creation.
    if not domain and "@" in email:
        domain = email.rsplit("@", 1)[1]
    if not professional_email(email, domain) or not meta.get("Email Contact Name"):
        return False
    if rank(meta.get("Email Contact Role")) >= 4:
        return False
    status = str(meta.get("Email Verification Status") or "").casefold()
    ownership = str(meta.get("Email Ownership Status") or "").casefold()
    if status not in ("verified", "valid") and not ownership.startswith("verified deliverable mailbox"):
        return False
    try:
        source = urlsplit(meta.get("Email Source") or "")
        checked = datetime.fromisoformat(meta.get("Email Ownership Checked At") or "")
        checked = checked.replace(tzinfo=timezone.utc) if checked.tzinfo is None else checked
        if checked > datetime.now(timezone.utc):
            return False
    except (ValueError, TypeError):
        return False
    return bool(source.scheme in ("https", "http") and source.hostname and meta.get("Email Evidence"))
