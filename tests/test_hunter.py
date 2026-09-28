import json

import pytest

from jobagent.enrich import hunter
from jobagent.outreach import founders


class Response:
    def __init__(self, status, data):
        self.status_code, self.data = status, data

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def fake_hunter(results, account=None):
    calls = []

    def get(url, params, timeout):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/account"):
            return Response(200, {"data": account or {}})
        return results.pop(0)
    return get, calls


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test")


PERSON = {"firstName": "Cai", "surname": "Tech", "domain": "beta.io"}


def test_only_verified_addresses_are_returned(tmp_path):
    get, _ = fake_hunter([Response(200, {"data": {"email": "cai@beta.io", "score": 96, "verification": {"status": "valid"}}}),
                          Response(200, {"data": {"email": "cai@beta.io", "verification": {"status": "accept_all"}}})])
    finder = hunter.make_finder(tmp_path / "usage.json", get=get)
    assert finder([PERSON]) == {("cai", "tech"): {"email": "cai@beta.io", "validationStatus": "Hunter verified: valid",
                                                  "overallScore": 96}}
    # A catch-all domain is not a verified address, but the search still counted.
    assert finder([PERSON]) == {}
    assert json.loads((tmp_path / "usage.json").read_text())["searches"] == 2


def test_local_monthly_cap_stops_before_calling_hunter(tmp_path):
    get, calls = fake_hunter([Response(200, {"data": {"email": "a@beta.io", "verification": {"status": "valid"}}})])
    finder = hunter.make_finder(tmp_path / "usage.json", monthly_limit=1, get=get)
    finder([PERSON])
    calls.clear()
    with pytest.raises(hunter.HunterError) as err:
        finder([PERSON])
    assert err.value.kind == "free_limit_reached" and calls == []


def test_account_with_no_searches_left_is_not_searched(tmp_path):
    get, calls = fake_hunter([], account={"requests": {"searches": {"used": 25, "available": 25}}})
    with pytest.raises(hunter.HunterError):
        hunter.make_finder(tmp_path / "usage.json", get=get)([PERSON])
    assert calls == ["account"]


def test_account_credits_are_respected(tmp_path):
    get, calls = fake_hunter([], account={"requests": {"credits": {"used": 50, "available": 50}}})
    with pytest.raises(hunter.HunterError):
        hunter.make_finder(tmp_path / "usage.json", get=get)([PERSON])
    assert calls == ["account"]


def test_usage_limit_response_stops_and_not_found_is_free(tmp_path):
    get, _ = fake_hunter([Response(404, {}), Response(200, {"data": {"email": None}}), Response(429, {})])
    finder = hunter.make_finder(tmp_path / "usage.json", get=get)
    assert finder([PERSON]) == {} and finder([PERSON]) == {}
    assert not (tmp_path / "usage.json").exists()
    with pytest.raises(hunter.HunterError):
        finder([PERSON])


DOMAIN_SEARCH = {"data": {"domain": "beta.io", "organization": "Beta", "emails": [
    {"value": "ana@beta.io", "first_name": "Ana", "last_name": "Boss", "position": "CEO", "confidence": 97,
     "verification": {"status": "valid"}, "linkedin": "https://www.linkedin.com/in/ana",
     "sources": [{"uri": "https://beta.io/team"}]},
    {"value": "cai@beta.io", "first_name": "Cai", "last_name": "Tech", "position": "Co-founder & CTO", "confidence": 95,
     "verification": {"status": "valid"}, "linkedin": "", "sources": []},
    {"value": "sam@beta.io", "first_name": "Sam", "last_name": "Ops", "position": "Chief Operating Officer",
     "verification": {"status": "valid"}, "sources": []},
    {"value": "dee@beta.io", "first_name": "Dee", "last_name": "Found", "position": "Founder",
     "verification": {"status": "accept_all"}, "sources": []}]}}


def test_domain_search_picks_verified_cto_first(tmp_path):
    get, calls = fake_hunter([Response(200, DOMAIN_SEARCH)])
    people = hunter.executives("Beta", "", tmp_path / "u.json", get=get)
    assert calls == ["account", "domain-search"]
    assert json.loads((tmp_path / "u.json").read_text())["searches"] == 1
    contact = founders.contact_from_executives("beta", people, domain_given=False)
    assert contact["Public Work Email"] == "cai@beta.io" and contact["Email Contact Role"] == "Co-founder & CTO"
    assert contact["Email Source"] == "https://beta.io"
    from jobagent.outreach.report_drafts import draft_recipient
    assert draft_recipient(contact) == "cai@beta.io"


def test_domain_search_by_name_rejects_a_different_organisation(tmp_path):
    other = json.loads(json.dumps(DOMAIN_SEARCH))
    other["data"]["organization"] = "Gamma Holdings"
    get, _ = fake_hunter([Response(200, other)])
    people = hunter.executives("Beta", "", tmp_path / "u.json", get=get)
    assert founders.contact_from_executives("beta", people, domain_given=False) == {}
    # With the company's own website given, the domain itself is the match.
    assert founders.contact_from_executives("beta", people, domain_given=True)["Public Work Email"] == "cai@beta.io"


def test_empty_domain_search_uses_no_credit(tmp_path):
    get, _ = fake_hunter([Response(200, {"data": {"domain": "beta.io", "emails": []}})])
    assert hunter.executives("Beta", "beta.io", tmp_path / "u.json", get=get) == []
    assert not (tmp_path / "u.json").exists()


def test_company_size_from_enrichment(tmp_path):
    get, _ = fake_hunter([Response(200, {"data": {"metrics": {"employees": "11-50"}}}),
                          Response(200, {"data": {"metrics": {"employeesCount": 42, "employees": "11-50"}}}),
                          Response(404, {})])
    assert hunter.company_size("beta.io", tmp_path / "u.json", get=get) == (11, 50)
    assert hunter.company_size("beta.io", tmp_path / "u.json", get=get) == (42, 42)
    assert hunter.company_size("beta.io", tmp_path / "u.json", get=get) is None


def test_daily_allowance_spreads_the_month(tmp_path, monkeypatch):
    import calendar
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    found = {"data": {"email": "a@beta.io", "verification": {"status": "valid"}}}
    get, _ = fake_hunter([Response(200, found) for _ in range(60)])
    finder = hunter.make_finder(tmp_path / "u.json", monthly_limit=50, get=get)
    allowance = -(-50 // days_left)
    for _ in range(allowance):
        finder([PERSON])
    with pytest.raises(hunter.HunterError):
        finder([PERSON])


def test_hunter_result_becomes_a_sendable_contact(tmp_path):
    get, _ = fake_hunter([Response(200, {"data": {"email": "cai@beta.io", "score": 96, "verification": {"status": "valid"}}})])
    people = [{"name": "Cai Tech", "title": "Co-founder & CTO", "url": "https://www.linkedin.com/in/cai",
               "source": "https://www.linkedin.com/in/cai"}]
    contact = founders.verified_contact(people, "beta.io", hunter.make_finder(tmp_path / "u.json", get=get))
    from jobagent.outreach.report_drafts import draft_recipient
    assert draft_recipient(contact) == "cai@beta.io"
    assert "Hunter verified: valid" in contact["Email Evidence"]
