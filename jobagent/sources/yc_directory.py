"""Hiring Y Combinator startups from the open YC directory mirror (free).

https://github.com/yc-oss/api publishes the public YC company directory as
static JSON, refreshed daily (checked 2026-09-28: 1,483 companies marked
hiring). Each record has the company's website, team size, locations,
industries, tags and one-line description, so startups can be chosen by size
and fit without any paid or rate-limited service.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

import httpx

HIRING = "https://yc-oss.github.io/api/companies/hiring.json"
# Software where QA/SDET work is central; hardware-only companies rank lower.
SOFTWARE = re.compile(r"\b(?:b2b|saas|software|developer tools|ai|artificial intelligence|machine learning|llm|"
                      r"fintech|payments|api|devops|security|infrastructure|analytics|data|enterprise|"
                      r"healthcare it|edtech|marketplace|consumer|e-commerce|automation|agents?)\b", re.I)
AI = re.compile(r"\b(?:ai|artificial intelligence|machine learning|llm|generative|agents?|chatbot|conversational|voice)\b", re.I)


def fetch_hiring(client=None):
    """Fetch public directory data, preserving HTTP failures as failures.

    A caller-owned client stays open; a client created here is always closed.
    A hiring flag indicates general hiring, never a confirmed QA opening.
    """
    if client is None:
        with httpx.Client(timeout=90, follow_redirects=True,
                          headers={"User-Agent": "JobAgent/1.0 (personal job search)"}) as owned:
            return fetch_hiring(owned)
    response = client.get(HIRING)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("YC directory response is not a list")
    return data


def domain_of(website):
    """Normalize the listed company website; never derive a domain from its name."""
    if not isinstance(website, str) or not website.strip():
        return ""
    website = website.strip()
    try:
        parsed = urlsplit(website if "://" in website or website.startswith("//") else "https://" + website)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if parsed.scheme not in ("", "http", "https") or parsed.username or parsed.password:
        return ""
    host = host[4:] if host.startswith("www.") else host
    if "." not in host or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host):
        return ""
    try:
        ipaddress.ip_address(host)
        return ""
    except ValueError:
        pass
    # A social-profile URL is not the startup's professional email domain.
    if any(host == domain or host.endswith("." + domain) for domain in
           ("linkedin.com", "facebook.com", "twitter.com", "x.com", "ycombinator.com")):
        return ""
    return host


def _strings(value):
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def candidates(companies, *, min_size=10, max_size=200, primary=(), places=()):
    """Active, hiring startups of min..max people, best fit first: software
    or AI products; the profile's preferred places (e.g. its home country) or
    remote, then its other places; then teams near 50 people, where a first or
    second QA hire is most likely."""
    top = [p.lower() for p in primary if isinstance(p, str) and len(p) > 3]
    wanted = [p.lower() for p in places if isinstance(p, str) and len(p) > 3]
    picked = []
    for c in companies:
        if not isinstance(c, dict):
            continue
        size = c.get("team_size")
        if not (c.get("isHiring") is True and c.get("status") == "Active" and _text(c.get("name"))
                and domain_of(c.get("website")) and type(size) is int and min_size <= size <= max_size):
            continue
        text = " ".join([_text(c.get("one_liner")), _text(c.get("industry")), _text(c.get("subindustry")),
                         *_strings(c.get("tags")), *_strings(c.get("industries"))])
        regions = " ".join([*_strings(c.get("regions")), _text(c.get("all_locations"))]).lower()
        score = (2 * bool(SOFTWARE.search(text)) + bool(AI.search(text))
                 + 3 * any(p in regions for p in top) + 2 * ("remote" in regions)
                 + any(p in regions for p in wanted))
        picked.append((score, abs(size - 50), c))
    picked.sort(key=lambda p: (-p[0], p[1], p[2].get("name", "")))
    result, seen = [], set()
    for score, _size, c in picked:
        domain = domain_of(c["website"])
        if domain in seen:
            continue
        seen.add(domain)
        website = _text(c["website"])
        if website.startswith("//"):
            website = "https:" + website
        elif "://" not in website:
            website = "https://" + website
        result.append({"name": c["name"].strip(), "slug": _text(c.get("slug")), "website": website,
                       "domain": domain, "team_size": c["team_size"], "one_liner": _text(c.get("one_liner")),
                       "batch": _text(c.get("batch")), "tags": _strings(c.get("tags")),
                       "industries": _strings(c.get("industries")), "regions": _strings(c.get("regions")),
                       "location": _text(c.get("all_locations")), "url": _text(c.get("url")),
                       "source": "Y Combinator directory", "is_hiring": True, "status": "Active", "fit": score})
    return result
