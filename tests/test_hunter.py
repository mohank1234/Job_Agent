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


def test_hunter_result_becomes_a_sendable_contact(tmp_path):
    get, _ = fake_hunter([Response(200, {"data": {"email": "cai@beta.io", "score": 96, "verification": {"status": "valid"}}})])
    people = [{"name": "Cai Tech", "title": "Co-founder & CTO", "url": "https://www.linkedin.com/in/cai",
               "source": "https://www.linkedin.com/in/cai"}]
    contact = founders.verified_contact(people, "beta.io", hunter.make_finder(tmp_path / "u.json", get=get))
    from jobagent.outreach.report_drafts import draft_recipient
    assert draft_recipient(contact) == "cai@beta.io"
    assert "Hunter verified: valid" in contact["Email Evidence"]
