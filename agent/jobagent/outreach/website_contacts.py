"""Step 1 of the email waterfall: what the company itself publishes.

Reads a few public pages of the company's own website (home, about, team,
leadership, company, contact, press) and returns
- leaders named on those pages (name + title), used to pick the decision-
  maker before any paid or free-credit lookup; and
- email addresses published there, on the company's own domain.

Nothing is guessed: an address is used only if it appears on the site. Only
a named leader's address or a leadership inbox (founders@, ceo@, cto@)
qualifies; info@, hello@, careers@ and other shared inboxes do not.
"""
from __future__ import annotations

import html as html_lib
import re

import httpx

from jobagent.outreach import founders

PAGES = ("", "/about", "/about-us", "/team", "/our-team", "/company", "/leadership", "/contact", "/contact-us",
         "/press")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Two to four capitalised words on one line (a name never spans a line break).
NAME = r"([A-Z][a-z]+(?:[-'][A-Z][a-z]+)?(?:[ \t]+[A-Z][a-z]*\.?){0,2}[ \t]+[A-Z][a-zA-Z'-]+)(?![ \t]*-?[ \t]*[Ff]ounder)"
TITLE = (r"((?:Co[-\s]?)?Founder(?:\s*(?:&|and|,|/)\s*(?:CEO|CTO|President|Chief [A-Z][a-z]+ Officer))?|CEO|CTO|"
         r"Chief (?:Executive|Technology|Technical) Officer|(?:VP|Vice President|Head) of Engineering|"
         r"Head of (?:QA|Quality)|Engineering Manager)")
# "Jane Doe, Co-founder & CEO" / "Jane Doe - CTO" / "Jane Doe | Founder" / "CEO Jane Doe"
NAME_THEN_TITLE = re.compile(NAME + r"[ \t]*(?:[,\-–—|:(]|\n)\s*" + TITLE)
# "CEO: Jane Doe" only on one line; across lines a title belongs to the name above it.
TITLE_THEN_NAME = re.compile(TITLE + r"[ \t]*[,\-–—|:]?[ \t]+" + NAME)


def _text(html):
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html or "")
    html = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|h\d|span|td)>", "\n", html)
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", html))


def _deobfuscate(text):
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", text, flags=re.I)
    return re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", text, flags=re.I)


def read_site(website, *, client=None, get=None):
    """{'pages': [...urls read], 'leaders': [{name, title, source}], 'emails': [{email, source}]}"""
    from jobagent.outreach.verification import get_public
    get = get or get_public
    base = (website or "").rstrip("/")
    domain = founders.professional_domain(base)
    found = {"pages": [], "leaders": [], "emails": []}
    if not domain:
        return found
    owned = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": "JobAgent/1.0 (personal job search)"})
    try:
        for path in PAGES:
            url = base + path
            try:
                html = get(client, url).text
            except Exception:
                continue
            found["pages"].append(url)
            mailto = re.findall(r'mailto:([^"\'?\s>]+)', html, re.I)
            text = _text(html)
            for email in dict.fromkeys([*mailto, *EMAIL.findall(_deobfuscate(text))]):
                email = email.strip().strip(".").lower()
                if email.rsplit("@", 1)[-1] in (domain, "www." + domain) or email.endswith("." + domain):
                    found["emails"].append({"email": email, "source": url})
            # People named on the homepage or press pages are often customers
            # quoted in testimonials; leaders come only from the company's own
            # team, about, company and leadership pages.
            if path in ("", "/contact", "/contact-us", "/press"):
                continue
            for pattern, order in ((NAME_THEN_TITLE, (1, 2)), (TITLE_THEN_NAME, (2, 1))):
                for m in pattern.finditer(text):
                    name, title = m.group(order[0]).strip(), m.group(order[1]).strip()
                    # "CEO of Shopify" on an investors/advisors list is another
                    # company's leader; keep only this company's own people.
                    other = re.match(r"[ \t]*(?:of|at|@)[ \t]+([A-Z][\w.&' -]{1,40})", text[m.end():])
                    if other and founders.key(other.group(1).split("\n")[0]).find(domain.split(".")[0]) < 0:
                        continue
                    if founders.is_decision_maker(title) and len(name.split()) <= 4:
                        found["leaders"].append({"name": name, "title": title, "url": url, "source": url})
    finally:
        if owned:
            client.close()
    found["emails"] = list({e["email"]: e for e in found["emails"]}.values())
    leaders = {}
    for person in found["leaders"]:
        leaders.setdefault(founders.key(person["name"]), person)  # first (name-then-title) match wins
    found["leaders"] = list(leaders.values())
    return found


def _name_tokens(name):
    return [t for t in re.findall(r"[a-z]+", (name or "").casefold()) if len(t) >= 2]


def published_contact(site, people, domain):
    """The best published address for a decision-maker, or {}.

    A named leader's address counts only if its local part is clearly theirs
    (their first name, last name, or first+last, as published); a leadership
    inbox (founders@, ceo@, cto@) counts as the company's own contact point.
    """
    domain = founders.professional_domain(domain)
    leaders = sorted([*people, *site.get("leaders", [])], key=lambda p: founders.rank(p.get("title")))
    for person in leaders:
        tokens = _name_tokens(person.get("name"))
        if len(tokens) < 2:
            continue
        first, last = tokens[0], tokens[-1]
        mine = {first, last, first + last, f"{first}.{last}", f"{first}_{last}", f"{first[0]}{last}",
                f"{first}{last[0]}", f"{first[0]}.{last}"}
        for item in site.get("emails", []):
            local = item["email"].split("@")[0]
            if local in mine and founders.professional_email(item["email"], domain):
                return _contact(item, person["name"], person["title"], domain)
    for item in site.get("emails", []):
        local = item["email"].split("@")[0]
        if not (founders.LEADERSHIP_INBOX.fullmatch(local) and founders.professional_email(item["email"], domain)):
            continue
        if local in ("ceo", "cto"):
            # ceo@ / cto@ reach that one person; name them when the site does.
            role = local.upper()
            holder = next((p for p in leaders if re.search(rf"\b{role}\b", p.get("title", ""), re.I)), None)
            return _contact(item, holder["name"] if holder else "", holder["title"] if holder else role, domain)
        return _contact(item, "", "Founders", domain)  # founders@ is a shared founders' inbox
    return {}


def _contact(item, name, title, domain):
    from jobagent.runtime import now_iso
    return {"Public Work Email": item["email"], "Email Source": item["source"],
            "Email Contact Name": name, "Email Contact Role": title,
            "Email Contact LinkedIn": "", "Email Provider": "COMPANY_WEBSITE",
            "Email Verification Status": "published", "Company Domain": domain,
            "Email Evidence": f"Published on the company's own website: {item['source']}",
            "Email Ownership Status": "Published by the company on its website; delivery not tested",
            "Email Ownership Checked At": now_iso()}
