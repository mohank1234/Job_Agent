import json

import pytest

from jobagent.enrich import email_finder
from jobagent.outreach import founders

COMPANY_PAGE = ("# Firecrawl\n\nFirecrawl employs 52 people (+147.6% YoY).\n## Workforce\n"
                "- Employees: 52\n- Company Size: 11-50 employees\n")


def test_parse_size_prefers_exact_count_then_band():
    assert founders.parse_size(COMPANY_PAGE) == (52, 52)
    assert founders.parse_size("- Company Size: 51-200 employees") == (51, 200)
    assert founders.parse_size("- Company Size: 1,001-5,000 employees") == (1001, 5000)
    assert founders.parse_size("no size here") is None


@pytest.mark.parametrize("size,expected", [((52, 52), True), ((11, 50), True), ((51, 200), True),
                                           ((2, 10), False), ((201, 500), False), ((9, 9), False), (None, False)])
def test_target_size(size, expected):
    assert founders.in_target(size) is expected


def test_company_size_only_trusts_the_matching_company_page():
    results = [{"url": "https://www.linkedin.com/in/someone", "snippet": "- Employees: 3"},
               {"url": "https://linkedin.com/company/other-co", "snippet": "# Other Co\n- Employees: 900"},
               {"url": "https://linkedin.com/company/firecrawl", "snippet": COMPANY_PAGE}]
    assert founders.company_size("Firecrawl", lambda *a, **k: results) == {
        "Employee Count": "52", "Employee Count Source": "https://linkedin.com/company/firecrawl"}
    assert founders.company_size("Firecrawl", lambda *a, **k: results[:2]) == {}


def test_profile_leaders_need_a_leader_headline_naming_the_company():
    results = [
        {"url": "https://www.linkedin.com/in/nick", "snippet": "# Nick Camara\n\nCo-Founder & CTO at Firecrawl (YC S22)"},
        {"url": "https://www.linkedin.com/in/eng", "snippet": "# Sam Eng\n\nSoftware Engineer at Firecrawl"},
        {"url": "https://www.linkedin.com/in/other", "snippet": "# Pat Other\n\nFounder at Othercorp"},
        {"url": "https://www.linkedin.com/posts/nick_activity-1", "snippet": "# Nick Camara\n\nCo-Founder at Firecrawl"},
    ]
    found = founders.profile_leaders("Firecrawl", lambda *a, **k: results)
    assert [p["name"] for p in found] == ["Nick Camara"]


def test_leaders_rank_cto_then_founder_then_ceo_and_merge_sources():
    row = {"Company": "Acme", "Job Link": "https://www.workatastartup.com/jobs/1",
           "Hiring Contacts": json.dumps([{"name": "Ana CEO", "title": "CEO", "url": ""},
                                          {"name": "Ben Builder", "title": None, "url": ""},
                                          {"name": "Recruiter Rae", "title": "Recruiter", "url": ""}])}
    meta = {"Manager Name": "Cai Tech", "Manager Role": "CTO", "Manager Source": "https://acme.com/team"}
    names = [p["name"] for p in founders.leaders(row, meta)]
    assert names[0] == "Cai Tech" and "Ana CEO" in names and "Recruiter Rae" not in names


def test_company_domain_uses_the_companys_own_site():
    urls = ["https://www.linkedin.com/company/firecrawl", "https://techcrunch.com/firecrawl-raises",
            "https://www.firecrawl.dev/blog/series-b"]
    assert founders.company_domain("Firecrawl", urls) == "firecrawl.dev"
    assert founders.company_domain("Bidgely Inc", ["https://www.bidgely.com/about"]) == "bidgely.com"
    assert founders.company_domain("Acme", ["https://unrelated.io"]) == ""


def test_verified_contact_requires_company_domain_and_a_source():
    people = [{"name": "Nick Camara", "title": "Co-Founder & CTO", "url": "https://www.linkedin.com/in/nick",
               "source": "https://www.linkedin.com/in/nick"}]
    finder = lambda batch: {("nick", "camara"): {"email": "nick@firecrawl.dev", "validationStatus": "valid", "overallScore": 95}}
    contact = founders.verified_contact(people, "firecrawl.dev", finder)
    assert contact["Public Work Email"] == "nick@firecrawl.dev"
    assert contact["Email Contact LinkedIn"] == "https://www.linkedin.com/in/nick"
    from jobagent.outreach.report_drafts import draft_recipient
    assert draft_recipient(contact) == "nick@firecrawl.dev"
    wrong_domain = lambda batch: {("nick", "camara"): {"email": "nick@gmail.com"}}
    assert founders.verified_contact(people, "firecrawl.dev", wrong_domain) == {}


def test_email_finder_keeps_only_safe_verified_addresses(monkeypatch):
    monkeypatch.setenv("APIFY_API_KEY", "test")
    batches = []

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    def post(url, params, json, timeout):
        batches.append(json["people"])
        return Response([
            {"firstName": p["firstName"], "surname": p["surname"], "email": f"{p['firstName']}@x.io",
             "isDeliverable": p["firstName"] != "Bad", "isSafeToSend": True,
             "isCatchAll": p["firstName"] == "Catch", "isRoleAccount": False} for p in json["people"]])
    people = [{"firstName": n, "surname": "Doe", "domain": "x.io"} for n in ("Ana", "Bad", "Catch", "Dee", "Eve", "Fay")]
    found = email_finder.find_emails(people, post=post)
    assert [len(b) for b in batches] == [5, 1]
    assert set(found) == {("ana", "doe"), ("dee", "doe"), ("eve", "doe"), ("fay", "doe")}


def test_yc_request_turns_off_repeat_suppression(monkeypatch):
    from jobagent.enrich import apify_yc
    monkeypatch.setenv("APIFY_API_KEY", "test")
    sent = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    def post(url, params, json, timeout):
        sent.update(json)
        return Response()
    monkeypatch.setattr(apify_yc.httpx, "post", post)
    apify_yc.fetch_yc_jobs([f"q{i}" for i in range(25)], max_items=1000)
    assert sent["dedupe"] == {"enabled": False, "key": ""}
    assert len(sent["queries"]) == 20 and sent["maxItems"] == 200


def test_yc_items_in_the_current_format_keep_location_and_contacts():
    from pathlib import Path
    from jobagent import morning
    from jobagent.profile import load_profile
    profile = load_profile(Path(__file__).resolve().parents[1] / "profile.yaml.example")
    item = {"id": "77", "url": "https://www.workatastartup.com/jobs/77", "title": "QA Automation Engineer",
            "company": "Acme", "locations": ["Bengaluru, India"], "workType": "remote",
            "description": "Build automated regression tests with Playwright and Python. " * 10,
            "hiringContacts": [{"name": "Ana Founder", "title": "Co-founder & CTO", "url": "https://www.linkedin.com/in/ana"}]}
    row = morning.assess_apify_leads([item], profile)[0]
    assert row["Location"] == "Bengaluru, India" and row["Workplace"] == "remote"
    assert json.loads(row["Hiring Contacts"])[0]["name"] == "Ana Founder"
