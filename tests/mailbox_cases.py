"""Job-alert email parsing cases.

Realistic alert-email HTML from each portal, including the noise that trips a
naive parser: unsubscribe links, tracking parameters, app-store banners, and
"view all jobs" navigation. Run with: python run.py mailbox-test --offline
"""

from __future__ import annotations

import email

from jobagent.sources.mailbox import parse_message

NAUKRI = """From: alerts@naukri.com
Subject: 12 new QA Engineer jobs in Hyderabad
Date: Mon, 18 Aug 2026 06:30:00 +0530
Content-Type: text/html

<html><body>
<a href="https://www.naukri.com/job-listings-senior-qa-automation-engineer-valuelabs-hyderabad-5-to-8-years-180826001234?src=jobsearchDesk&utm_source=alert">Senior QA Automation Engineer - ValueLabs</a>
<a href="https://www.naukri.com/job-listings-sdet-highradius-hyderabad-4-to-7-years-180826005678?trk=alert">SDET - HighRadius</a>
<a href="https://www.naukri.com/job-listings-qa-engineer-darwinbox-hyderabad-3-to-6-years-180826009999">QA Engineer at Darwinbox</a>
<a href="https://www.naukri.com/mnjuser/profile">Update your profile</a>
<a href="https://www.naukri.com/unsubscribe?id=xyz">Unsubscribe</a>
<a href="https://play.google.com/store/apps/details?id=naukriApp">Download the app</a>
</body></html>
"""

LINKEDIN = """From: jobalerts-noreply@linkedin.com
Subject: "QA Engineer" : 8 new jobs
Date: Mon, 18 Aug 2026 07:00:00 +0000
Content-Type: text/html

<html><body>
<a href="https://www.linkedin.com/comm/jobs/view/3912345678/?trackingId=abc%3D%3D&refId=xyz&midToken=AQE">Senior SDET at Postman</a>
<a href="https://www.linkedin.com/jobs/view/3987654321?trk=eml-jobs_jymbii-header-0">LLM Evaluation Engineer - Scale AI</a>
<a href="https://www.linkedin.com/comm/jobs/search?keywords=qa">See all jobs</a>
<a href="https://www.linkedin.com/psettings/email">Unsubscribe</a>
</body></html>
"""

INDEED = """From: alert@indeed.com
Subject: New QA jobs for you
Date: Mon, 18 Aug 2026 05:00:00 +0000
Content-Type: text/html

<html><body>
<a href="https://in.indeed.com/rc/clk?jk=abc123&fccid=xyz&utm_source=jobseeker_emails">QA Automation Engineer - Tech Mahindra</a>
<a href="https://in.indeed.com/viewjob?jk=def456">Test Engineer, AI Platform | Cognizant</a>
<a href="https://in.indeed.com/account/settings">Email settings</a>
</body></html>
"""

MULTIPART = """From: alerts@instahyre.com
Subject: 3 curated matches
Date: Mon, 18 Aug 2026 04:00:00 +0000
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="XX"

--XX
Content-Type: text/plain

Plain text version with no links.
--XX
Content-Type: text/html

<html><body>
<a href="https://www.instahyre.com/job-12345/qa-lead-zenoti">QA Lead at Zenoti</a>
<a href="https://www.instahyre.com/candidate/settings">Preferences</a>
</body></html>
--XX--
"""

NOISE = """From: newsletter@somesite.com
Subject: Weekly tech digest
Date: Mon, 18 Aug 2026 04:00:00 +0000
Content-Type: text/html

<html><body>
<a href="https://example.com/blog/testing-tips">10 testing tips</a>
<a href="https://github.com/some/repo">A repo</a>
</body></html>
"""

# Real Naukri recruiter broadcasts (subject-line postings) plus the marketing
# mail that must never be mistaken for a job. Captured from a live inbox.
def _naukri_broadcast(subject, sender):
    """Naukri's single-job recruiter mail: title in the subject, company in the
    sender name, and only an opaque redirect in the body."""
    return (
        "From: {s} <x@naukri.com>\n"
        "Subject: {j}\n"
        "Date: Tue, 18 Aug 2026 09:46:50 +0530\n"
        "Content-Type: text/html\n\n"
        "<html><body>"
        "<a href='https://www.naukri.com?utm_source=naukriLogo'>logo</a>"
        "<a href='https://www.naukri.com/durl/nEZb'>Apply</a>"
        "<a href='https://www.naukri.com/mnjuser/settings/communication'>Unsubscribe</a>"
        "</body></html>\n"
    ).format(s=sender, j=subject)


NAUKRI_SDET = _naukri_broadcast(
    "Job | Senior SDET Test Engineer in [Hyderabad]",
    "CENTIFIC GLOBAL TECHNOLOGIES INDIA PRIVATE LIMITED")
NAUKRI_WALKIN = _naukri_broadcast(
    "Walk-in interview | Automation QA Engineer in Hyderabad", "Carrier")
NAUKRI_MULTI = _naukri_broadcast(
    "Job | Performance Test Engineer in Hyderabad, Bengaluru, Chennai",
    "Nityo Infotech")
NAUKRI_REMOTE = _naukri_broadcast(
    "Job | QA Automation Enginner - AI (Remote) based in India",
    "Five Data Products And Solutions")
NAUKRI_MARKETING_1 = _naukri_broadcast(
    "Congrats Krishna Mohan B. You can now boost your jobsearch with flat 10% discount",
    "Naukri FastForward")
NAUKRI_MARKETING_2 = _naukri_broadcast(
    "This resume format could get you shortlisted", "Naukri MINIs")

CUTSHORT_OTP = (
    "From: Cutshort Team <notify@cutshort.io>\n"
    "Subject: KrishnaMohan, here is your OTP to verify your email address\n"
    "Date: Sat, 15 Aug 2026 21:43:31 +0000\n"
    "Content-Type: text/html\n\n"
    "<html><body><a href='https://cutshort.io/api/tracking/email-tracking/click"
    "?templateId=verify-email-otp'>Verify</a></body></html>\n"
)

CASES = [
    ("naukri", NAUKRI, 3, ["Senior QA Automation Engineer", "SDET", "QA Engineer"]),
    ("linkedin", LINKEDIN, 2, ["Senior SDET", "LLM Evaluation Engineer"]),
    ("indeed", INDEED, 2, ["QA Automation Engineer", "Test Engineer, AI Platform"]),
    ("instahyre-multipart", MULTIPART, 1, ["QA Lead"]),
    ("unrelated-newsletter", NOISE, 0, []),
    # --- real Naukri recruiter broadcasts (title lives in the SUBJECT) -----
    ("naukri-broadcast-sdet",   NAUKRI_SDET,   1, ["Senior SDET Test Engineer"]),
    ("naukri-broadcast-walkin", NAUKRI_WALKIN, 1, ["Automation QA Engineer"]),
    ("naukri-broadcast-multi",  NAUKRI_MULTI,  1, ["Performance Test Engineer"]),
    ("naukri-broadcast-remote", NAUKRI_REMOTE, 1, ["QA Automation Enginner - AI (Remote)"]),
    # --- portal marketing must never become a job --------------------------
    ("naukri-marketing-discount", NAUKRI_MARKETING_1, 0, []),
    ("naukri-marketing-resume",   NAUKRI_MARKETING_2, 0, []),
    ("cutshort-otp",              CUTSHORT_OTP,       0, []),
]


def run_cases() -> list[dict]:
    results = []
    for name, raw, expected_n, expected_titles in CASES:
        msg = email.message_from_string(raw)
        jobs = parse_message(msg, set())
        titles = [j.title for j in jobs]
        ok = len(jobs) == expected_n and all(
            any(t == got for got in titles) for t in expected_titles
        )
        # Tracking parameters must be stripped from every URL.
        clean = all(
            not any(p in (j.url or "") for p in ("utm_", "trk=", "trackingId", "midToken", "refId"))
            for j in jobs
        )
        results.append({
            "case": name,
            "expected": expected_n,
            "got": len(jobs),
            "titles": titles,
            "companies": [j.company for j in jobs],
            "urls_clean": clean,
            "ok": ok and clean,
        })
    return results
