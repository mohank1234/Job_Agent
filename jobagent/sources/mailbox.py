"""Job-alert email ingestion over IMAP.

This is the legitimate route to Naukri, LinkedIn, Indeed, Instahyre, Hirist,
Foundit and Cutshort. None of them has a public API and all of them prohibit
scraping — but all of them will happily email you a saved-search alert. Those
emails are yours, sitting in your own mailbox. Reading your own inbox breaks
nobody's terms and cannot get an account banned.

Setup:
  1. Create a saved search on each portal and set the alert to daily.
  2. Optionally filter them into one folder, e.g. "JobAlerts".
  3. Create an app password for your mail account (Gmail: Google Account ->
     Security -> App passwords). Never use your real password.
  4. Put it in an environment variable and point config.yaml at it.

Then check what it can see before trusting it:
     python run.py mailbox-test
"""

from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

from ..models import Job, clean_html, infer_workplace

# Per-portal job-link patterns. A job alert links to many things (unsubscribe,
# profile, ads); only these shapes are actual postings.
JOB_LINK_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("naukri",     re.compile(r"https?://[^\s\"'<>]*naukri\.com/job-listings[^\s\"'<>]*", re.I)),
    ("naukri",     re.compile(r"https?://[^\s\"'<>]*naukri\.com/jobs?/[^\s\"'<>]*", re.I)),
    ("linkedin",   re.compile(r"https?://[^\s\"'<>]*linkedin\.com/(?:comm/)?jobs/view/[^\s\"'<>]*", re.I)),
    ("indeed",     re.compile(r"https?://[^\s\"'<>]*indeed\.com/(?:rc/clk|viewjob|job)[^\s\"'<>]*", re.I)),
    ("instahyre",  re.compile(r"https?://[^\s\"'<>]*instahyre\.com/[^\s\"'<>]*job[^\s\"'<>]*", re.I)),
    ("hirist",     re.compile(r"https?://[^\s\"'<>]*hirist\.(?:com|tech)/j/[^\s\"'<>]*", re.I)),
    ("foundit",    re.compile(r"https?://[^\s\"'<>]*foundit\.in/job[^\s\"'<>]*", re.I)),
    ("cutshort",   re.compile(r"https?://[^\s\"'<>]*cutshort\.io/job[^\s\"'<>]*", re.I)),
    ("glassdoor",  re.compile(r"https?://[^\s\"'<>]*glassdoor\.[a-z.]+/job-listing[^\s\"'<>]*", re.I)),
    ("timesjobs",  re.compile(r"https?://[^\s\"'<>]*timesjobs\.com/job-detail[^\s\"'<>]*", re.I)),
    ("wellfound",  re.compile(r"https?://[^\s\"'<>]*wellfound\.com/jobs/[^\s\"'<>]*", re.I)),
    ("ycombinator", re.compile(r"https?://[^\s\"'<>]*workatastartup\.com/jobs/[^\s\"'<>]*", re.I)),
]

# <a href="...">anchor text</a> — the anchor text is usually the job title.
ANCHOR = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)

# ---------------------------------------------------------------------------
# Subject-line postings
#
# Naukri's most valuable mail is not a digest of links — it is a recruiter
# broadcast about ONE job, where the title and location live in the SUBJECT
# and the company is the sender's display name. The body only contains an
# opaque redirect (naukri.com/durl/xxxx), so anchor parsing finds nothing.
# Real examples:
#   "Job | Senior SDET Test Engineer in [Hyderabad]"
#   "Walk-in interview | Automation QA Engineer in Hyderabad"
#   "Job | QA Automation Enginner - AI (Remote) based in India"
# ---------------------------------------------------------------------------
RE_SUBJECT_JOB = re.compile(
    r"^\W*(?:job|walk\s*-?\s*in(?:\s+interview)?|hiring|opening)\s*[|:]\s*"
    r"(?P<title>.+?)\s+(?:based\s+in|in)\s+(?P<loc>.+?)\s*$",
    re.I,
)

# Senders that are the portal marketing to you, not a recruiter with a job.
RE_MARKETING_SENDER = re.compile(
    r"naukri\s*(minis|fastforward|fast forward)|"
    r"naukri\.com\s*-|jobseeker services|premium|subscription", re.I,
)
RE_MARKETING_SUBJECT = re.compile(
    r"discount|resume format|profile (views|performance)|nvite|"
    r"rank higher|reminder for|upgrade|webinar|course|certification|"
    r"otp|verify your email|password|welcome to|newsletter", re.I,
)

# Naukri broadcast links. The apply link carries `mid=<id>`, unique per job.
# The /durl/ short link looks like a job link but is the SAME string in every
# email (an app-install redirect) — using it as the identity silently collapsed
# 18 distinct jobs into 1.
RE_NAUKRI_APPLY = re.compile(
    r"https?://my\.naukri\.com/[^\s\"'<>]*?mid=(?P<mid>\d+)[^\s\"'<>]*", re.I)
RE_NAUKRI_DURL = re.compile(r"https?://(?:www\.)?naukri\.com/durl/\w+", re.I)

_TRACKING = re.compile(r"[?&](utm_[^=]+|trk|trackingId|refId|midToken|eBP|lipi)=[^&]*", re.I)


def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return str(value)


def _body(msg) -> str:
    """Prefer the HTML part: alert emails put the job cards there."""
    html, text = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            decoded = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except (LookupError, ValueError):
            continue
        if ctype == "text/html":
            html += decoded
        else:
            text += decoded
    return html or text


def _clean_url(url: str) -> str:
    url = url.replace("&amp;", "&")
    return _TRACKING.sub("", url).rstrip("?&")


def _portal_for(url: str) -> str | None:
    for name, pattern in JOB_LINK_PATTERNS:
        if pattern.match(url) or pattern.search(url):
            return name
    return None


def _title_company(anchor_text: str) -> tuple[str, str]:
    """Alert anchors read like 'Senior QA Engineer - Acme Corp' or
    'QA Engineer at Acme'. Split on the usual separators."""
    text = clean_html(anchor_text)
    text = re.sub(r"\s+", " ", text).strip()
    for sep in (" at ", " - ", " – ", " | ", ", "):
        if sep in text:
            title, _, company = text.partition(sep)
            return title.strip(), company.strip()
    return text, ""


def _sender_name(from_header: str) -> str:
    """Display name from a From header: the recruiter's company."""
    name = (from_header or "").split("<")[0].strip().strip('"').strip()
    return re.sub(r"\s+", " ", name)


def _parse_subject_job(subject: str, sender: str, body: str,
                       posted, seen_urls: set[str]) -> Job | None:
    """One posting carried entirely in the subject line."""
    if RE_MARKETING_SENDER.search(sender) or RE_MARKETING_SUBJECT.search(subject):
        return None
    match = RE_SUBJECT_JOB.match(subject.strip())
    if not match:
        return None

    title = re.sub(r"\s+", " ", match.group("title")).strip(" -|")
    location = match.group("loc").strip()
    # Naukri wraps single cities in brackets: "[Hyderabad]"
    location = location.strip("[]() ").strip()
    if not title or len(title) < 4:
        return None

    # Prefer the per-job apply link. Only it identifies the posting.
    url = ""
    apply_link = RE_NAUKRI_APPLY.search(body or "")
    if apply_link:
        url = f"https://www.naukri.com/job-listings-{apply_link.group('mid')}"
    else:
        for raw_url, _text in ANCHOR.findall(body or ""):
            cleaned = _clean_url(raw_url)
            if _portal_for(cleaned) and not RE_NAUKRI_DURL.match(cleaned):
                url = cleaned
                break
        if not url:
            found = RE_NAUKRI_DURL.search(body or "")
            url = found.group(0) if found else ""
    if not url:
        return None

    # Identity is title+company, not the URL: Naukri reuses one short link
    # across every broadcast, so URL-based dedupe would drop real jobs.
    key = f"subject:{title.lower()}|{_sender_name(sender).lower()}"
    if key in seen_urls:
        return None
    seen_urls.add(key)

    portal = _portal_for(url) or "naukri"
    return Job(
        source=f"mail/{portal}",
        company=_sender_name(sender) or portal,
        title=title,
        url=url,
        location=location,
        workplace=infer_workplace(location, title, subject),
        description=(
            f"Recruiter email from {_sender_name(sender)} via {portal}. "
            f"Subject: {subject}. Location: {location}. "
            f"Open the link for the full description."
        ),
        posted_at=posted,
    )


def parse_message(msg, seen_urls: set[str]) -> list[Job]:
    """Pull postings out of one alert email.

    Two shapes, both real:
      - a recruiter broadcast about ONE job, title in the subject line
      - a digest of several jobs, each an <a> link with the title as its text
    """
    body = _body(msg)
    if not body:
        return []
    subject = _decode(msg.get("Subject"))
    sender = _decode(msg.get("From"))
    date_hdr = msg.get("Date")
    try:
        posted = email.utils.parsedate_to_datetime(date_hdr) if date_hdr else None
    except (TypeError, ValueError):
        posted = None

    # A subject-line posting is a whole email about one job. Take it and stop:
    # its body links are apply/redirect URLs, not other jobs.
    single = _parse_subject_job(subject, sender, body, posted, seen_urls)
    if single:
        return [single]

    if RE_MARKETING_SENDER.search(sender) or RE_MARKETING_SUBJECT.search(subject):
        return []

    jobs: list[Job] = []
    for raw_url, anchor_text in ANCHOR.findall(body):
        url = _clean_url(raw_url)
        portal = _portal_for(url)
        if not portal or url in seen_urls:
            continue
        title, company = _title_company(anchor_text)
        if not title or len(title) < 4 or len(title) > 140:
            continue
        # Anchors that are plainly navigation, not a posting.
        if re.search(r"unsubscribe|view all|see all|update|settings|privacy|"
                     r"download|app store|google play|profile|login|sign in",
                     title, re.I):
            continue
        seen_urls.add(url)
        jobs.append(
            Job(
                source=f"mail/{portal}",
                company=company or portal,
                title=title,
                url=url,
                location="",
                workplace=infer_workplace(title, subject),
                description=(
                    f"Job alert from {portal} (email: {subject}). "
                    f"Sender: {sender}. Full description is on the posting page."
                ),
                posted_at=posted,
            )
        )
    return jobs


def fetch_mailbox(cfg: dict) -> tuple[list[Job], list[str]]:
    """Read job-alert emails. Returns (jobs, errors). Read-only: never marks
    messages read, never deletes, never moves anything."""
    errors: list[str] = []
    if not cfg or not cfg.get("enabled"):
        return [], errors

    host = cfg.get("host", "imap.gmail.com")
    port = int(cfg.get("port", 993))
    user = cfg.get("user") or os.environ.get("JOBAGENT_MAIL_USER", "")
    password = os.environ.get(cfg.get("password_env", "JOBAGENT_MAIL_PASSWORD"), "")
    folder = cfg.get("folder", "INBOX")
    days = int(cfg.get("max_age_days", 7))

    if not user or not password:
        return [], [
            f"mailbox: set sources.mailbox.user and the {cfg.get('password_env','JOBAGENT_MAIL_PASSWORD')} "
            f"environment variable (use an app password, not your real one)"
        ]

    jobs: list[Job] = []
    seen_urls: set[str] = set()
    try:
        conn = imaplib.IMAP4_SSL(host, port)
        try:
            conn.login(user, password)
            # readonly=True: the mailbox is never modified.
            conn.select(folder, readonly=True)
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")

            # Filter on the SERVER by sender. Downloading every message in the
            # window and testing locally means fetching hundreds of unrelated
            # mails over IMAP, which takes minutes; this takes seconds.
            senders = cfg.get("senders") or [
                "naukri", "linkedin", "indeed", "instahyre", "hirist",
                "foundit", "cutshort", "glassdoor", "timesjobs", "monster",
                "shine", "wellfound", "workatastartup",
            ]
            ids: list[bytes] = []
            seen_ids: set[bytes] = set()
            for sender_frag in senders:
                status, data = conn.search(
                    None, f'(SINCE {since} FROM "{sender_frag}")'
                )
                if status != "OK":
                    continue
                for mid in (data[0] or b"").split():
                    if mid not in seen_ids:
                        seen_ids.add(mid)
                        ids.append(mid)
            if not ids:
                return [], [
                    f"mailbox: no job-alert emails from {len(senders)} known "
                    f"portals in the last {days} days in folder {folder!r}"
                ]

            for msg_id in ids[-int(cfg.get("max_messages", 400)):]:
                status, payload = conn.fetch(msg_id, "(RFC822)")
                if status != "OK" or not payload or not payload[0]:
                    continue
                msg = email.message_from_bytes(payload[0][1])
                jobs.extend(parse_message(msg, seen_urls))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as exc:
        errors.append(f"mailbox: IMAP error: {exc}")
    except OSError as exc:
        errors.append(f"mailbox: connection failed: {exc}")

    return jobs, errors
