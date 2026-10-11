"""Hacker News "Ask HN: Who is hiring?" - free, official API, no key.

Each month's thread holds a few hundred posts written by the hiring
companies themselves, usually "Company | Role | Location | ..." followed by
details, careers links and often an address to apply to. Two free uses:
- careers-board links (Ashby/Lever/Greenhouse) join the watch list;
- an address a company published in its own post, for applicants, is a
  sourced hiring contact for that company.
API: https://hn.algolia.com/api (public, documented).
"""
from __future__ import annotations

import html
import re

import httpx

from jobagent.outreach.verification import board_ref

API = "https://hn.algolia.com/api/v1"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
URL = re.compile(r"https?://[^\s<>\"')]+")
WEBMAIL = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me", "protonmail.com", "icloud.com")


def plain(text):
    text = re.sub(r"<p>|<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>.*?</a>', r" \1 ", text, flags=re.I | re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def deobfuscate(text):
    """"jobs [at] acme [dot] com" and similar -> jobs@acme.com."""
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*|\s+at\s+(?=[\w-]+\s*[\[(]?\s*dot)", "@", text, flags=re.I)
    return re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*|\s+dot\s+(?=[a-z]{2,}\b)", ".", text, flags=re.I)


def parse_post(item):
    text = plain(item.get("text"))
    first = next((line for line in text.splitlines() if line.strip()), "")
    company = re.split(r"\s*[|–—-]\s+|\s+\(", first.strip(), maxsplit=1)[0].strip()[:80]
    urls = list(dict.fromkeys(u.rstrip(".,;") for u in URL.findall(text)))
    emails = [e for e in dict.fromkeys(EMAIL.findall(deobfuscate(text)))
              if e.split("@")[1].lower() not in WEBMAIL and not re.match(r"(?:no-?reply|privacy|security|support|sales)@", e, re.I)]
    return {"id": str(item.get("id")), "company": company, "text": text, "urls": urls, "emails": emails,
            "boards": [u for u in urls if board_ref(u)]}


def latest_posts(months=2, client=None):
    """Top-level posts from the latest `months` "Who is hiring?" threads."""
    client = client or httpx.Client(timeout=60, headers={"User-Agent": "JobAgent/1.0 (personal job search)"})
    stories = client.get(f"{API}/search_by_date", params={"tags": "story,author_whoishiring", "hitsPerPage": 12}).json()
    threads = [h for h in stories.get("hits", []) if (h.get("title") or "").startswith("Ask HN: Who is hiring?")][:months]
    posts = []
    for thread in threads:
        item = client.get(f"{API}/items/{thread['objectID']}").json()
        month = re.search(r"\(([^)]+)\)", thread["title"])
        for child in item.get("children") or []:
            if child.get("text"):
                posts.append({**parse_post(child), "thread": month.group(1) if month else thread["title"]})
    return posts


def contact_for(company, board_slugs, posts):
    """A hiring address the company published in its own post, matched by
    careers-board slug first, then by company name."""
    wanted = re.sub(r"[^a-z0-9]", "", company.casefold())
    for post in posts:
        slugs = {board_ref(u)[1].casefold() for u in post["boards"]}
        by_board = bool(slugs & {s.casefold() for s in board_slugs})
        by_name = wanted and re.sub(r"[^a-z0-9]", "", post["company"].casefold()) == wanted
        if (by_board or by_name) and post["emails"]:
            email = post["emails"][0]
            return {"Public Work Email": email,
                    "Email Source": f"https://news.ycombinator.com/item?id={post['id']}",
                    "Email Contact Name": "", "Email Contact Role": "Hiring contact named in the company's own post",
                    "Email Evidence": f"Published by {post['company']} in Hacker News \"Who is hiring?\" ({post['thread']}) "
                                      "as the address for applicants; exact address present",
                    "Email Ownership Status": "Published hiring contact; delivery not tested"}
    return {}
