from datetime import date
from types import SimpleNamespace

import pytest

from jobagent.enrich import credits as credits_mod
from jobagent.enrich import hunter, prospeo, tomba
from jobagent.enrich.credits import Credits, LimitReached
from jobagent.outreach import startup_pipeline, website_contacts


class Resp:
    def __init__(self, status, data):
        self.status_code, self.data = status, data

    def json(self):
        return self.data


# ---------------------------------------------------------------- providers

def test_prospeo_accepts_only_verified_domain_email_and_counts_found_emails(tmp_path, monkeypatch):
    monkeypatch.setenv("PROSPEO_API_KEY", "k")
    credits = Credits(tmp_path / "p.json", 100, "Prospeo")
    sent = []
    def post(url, headers, timeout, json):
        sent.append(json)
        return Resp(200, {"error": False, "person": {"email": {"email": "cai@beta.io", "status": "VERIFIED"}}})
    assert prospeo.find_email("Cai", "Tech", "beta.io", credits, post=post)[0] == "cai@beta.io"
    assert sent[0]["only_verified_email"] is True and sent[0]["data"]["company_website"] == "beta.io"
    no_match = lambda *a, **k: Resp(400, {"error": True, "error_code": "NO_MATCH"})
    assert prospeo.find_email("Cai", "Tech", "beta.io", credits, post=no_match)[0] == ""
    unverified = lambda *a, **k: Resp(200, {"person": {"email": {"email": "cai@beta.io", "status": "UNVERIFIED"}}})
    assert prospeo.find_email("Cai", "Tech", "beta.io", credits, post=unverified)[0] == ""
    assert credits.allowance_today()[1]["used"] == 2  # the verified and the unverified email; not the no-match
    with pytest.raises(LimitReached):
        prospeo.find_email("Cai", "Tech", "beta.io", credits,
                           post=lambda *a, **k: Resp(400, {"error": True, "error_code": "INSUFFICIENT_CREDITS"}))


def test_tomba_counts_every_call_and_accepts_only_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("TOMBA_API_KEY", "k")
    monkeypatch.setenv("TOMBA_SECRET", "s")
    credits = Credits(tmp_path / "t.json", 25, "Tomba")
    valid = lambda *a, **k: Resp(200, {"data": {"email": "cai@beta.io", "score": 91, "verification": {"status": "valid"}}})
    risky = lambda *a, **k: Resp(200, {"data": {"email": "cai@beta.io", "verification": {"status": "accept_all"}}})
    assert tomba.find_email("Cai", "Tech", "beta.io", credits, get=valid)[0] == "cai@beta.io"
    assert tomba.find_email("Cai", "Tech", "beta.io", credits, get=risky)[0] == ""
    assert credits.allowance_today()[1]["used"] == 2
    with pytest.raises(LimitReached):
        tomba.find_email("Cai", "Tech", "beta.io", credits, get=lambda *a, **k: Resp(429, {}))


def test_credits_follow_the_providers_own_balance_and_renewal_date(tmp_path):
    from datetime import datetime, timedelta, timezone
    reset = (datetime.now(timezone.utc) + timedelta(days=10)).date()
    reads = []
    c = Credits(tmp_path / "p.json", 100, "Prospeo", account=lambda: reads.append(1) or {"left": 30, "reset": reset})
    for _ in range(3):  # 30 left over 10 days: 3 today, however few days the calendar month has left
        c.check()
        c.spend()
    with pytest.raises(LimitReached, match=r"\(3\)"):
        c.check()
    assert len(reads) == 1  # the provider's balance is read once per run
    none_left = Credits(tmp_path / "q.json", 100, "Prospeo", account=lambda: {"left": 0, "reset": reset})
    with pytest.raises(LimitReached):
        none_left.check()


def test_credits_spread_the_month_and_stop_at_the_limit(tmp_path):
    import calendar
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    c = Credits(tmp_path / "c.json", 10 * days_left, "X")
    for _ in range(10):
        c.check()
        c.spend()
    with pytest.raises(LimitReached):
        c.check()


# ----------------------------------------------------------------- waterfall

ROW = {"Company": "Beta", "Domain": "beta.io", "Website": "https://beta.io", "Startup Source": "YC", "Job Link": "x"}


def run(monkeypatch, tmp_path, *, site=None, prospeo_result=None, hunter_contact=None, tomba_result=None, rows=None,
        metadata=None, lookups=None):
    calls = []
    monkeypatch.setattr(prospeo, "api_key", lambda: "k")
    monkeypatch.setattr(tomba, "active", lambda: True)
    monkeypatch.setattr(hunter, "api_key", lambda: "k")
    monkeypatch.setattr(prospeo, "find_email", lambda *a, **k: calls.append("prospeo") or (prospeo_result or ("", "no match")))
    monkeypatch.setattr(tomba, "find_email", lambda *a, **k: calls.append("tomba") or (tomba_result or ("", "no match")))
    monkeypatch.setattr(hunter, "make_finder", lambda *a, **k: (
        lambda people: calls.append("hunter") or ({("cai", "tech"): {"email": hunter_contact, "validationStatus": "valid"}}
                                                  if hunter_contact else {})))
    monkeypatch.setattr(hunter, "executives", lambda *a, **k: calls.append("hunter-domain") or [])
    site = site or {"pages": ["https://beta.io/team"], "emails": [],
                    "leaders": [{"name": "Cai Tech", "title": "Co-founder & CTO", "url": "https://beta.io/team",
                                 "source": "https://beta.io/team"}]}
    log = []
    metadata = metadata if metadata is not None else {r["Company"].casefold(): {} for r in (rows or [ROW])}
    startup_pipeline.enrich_contacts(rows or [ROW], metadata, {}, lookups if lookups is not None else {}, {},
                                     tmp_path, date(2026, 10, 2), search=None, progress=log.append,
                                     read_site=lambda url: site)
    return calls, metadata, log


def test_website_email_wins_and_no_credit_is_spent(monkeypatch, tmp_path):
    site = {"pages": ["https://beta.io/team"], "emails": [{"email": "cai@beta.io", "source": "https://beta.io/team"}],
            "leaders": [{"name": "Cai Tech", "title": "Co-founder & CTO", "url": "", "source": "https://beta.io/team"}]}
    calls, meta, log = run(monkeypatch, tmp_path, site=site)
    assert calls == [] and meta["beta"]["Email Provider"] == "COMPANY_WEBSITE"
    assert "website: published cai@beta.io" in log[0]


def test_prospeo_second_and_the_waterfall_stops(monkeypatch, tmp_path):
    calls, meta, log = run(monkeypatch, tmp_path, prospeo_result=("cai@beta.io", "VERIFIED"),
                           hunter_contact="cai@beta.io")
    assert calls == ["prospeo"] and meta["beta"]["Email Provider"] == "PROSPEO"
    assert meta["beta"]["Email Contact Name"] == "Cai Tech"


def test_hunter_third_then_tomba_last(monkeypatch, tmp_path):
    calls, meta, _ = run(monkeypatch, tmp_path, hunter_contact="cai@beta.io")
    assert calls == ["prospeo", "hunter"] and meta["beta"]["Email Provider"] == "HUNTER"
    calls, meta, _ = run(monkeypatch, tmp_path / "2", tomba_result=("cai@beta.io", "valid (score 90)"))
    assert calls == ["prospeo", "hunter", "tomba"] and meta["beta"]["Email Provider"] == "TOMBA"


def test_nothing_verified_is_recorded_and_not_retried_for_a_week(monkeypatch, tmp_path):
    lookups = {}
    calls, meta, log = run(monkeypatch, tmp_path, lookups=lookups)
    assert calls == ["prospeo", "hunter", "tomba"] and meta["beta"]["Email Provider"] == "NO_VERIFIED_EMAIL"
    assert "Prospeo: no match" in log[0] and log[0].endswith("=> NO_VERIFIED_EMAIL")
    calls, _meta, _ = run(monkeypatch, tmp_path, lookups=lookups)
    assert calls == []


def test_without_a_name_only_hunter_domain_search_is_spent(monkeypatch, tmp_path):
    empty = {"pages": ["https://beta.io"], "emails": [], "leaders": []}
    calls, _meta, log = run(monkeypatch, tmp_path, site=empty)
    assert calls == ["hunter-domain"]
    assert "Prospeo: skipped (no decision-maker name" in log[0] and "Tomba: skipped (no decision-maker name" in log[0]


def test_one_address_is_never_used_for_two_companies(monkeypatch, tmp_path):
    rows = [ROW, {**ROW, "Company": "Gamma", "Domain": "beta.io", "Website": "https://beta.io"}]
    lookups = {}
    calls, meta, log = run(monkeypatch, tmp_path, prospeo_result=("cai@beta.io", "VERIFIED"), rows=rows, lookups=lookups)
    assert calls == ['prospeo']  # no second paid lookup for the alias
    assert lookups['beta.io']['contact']['Public Work Email'] == 'cai@beta.io'
    assert meta["beta"]["Public Work Email"] == "cai@beta.io"
    assert meta["gamma"]["Email Provider"] == "NO_VERIFIED_EMAIL"
    assert any("already used for another company" in line for line in log)


@pytest.mark.parametrize('partial', [None, 'TOMBA_API_KEY', 'TOMBA_SECRET'])
def test_missing_optional_keys_skip_without_charges_and_keep_hunter(monkeypatch, tmp_path, partial):
    if partial:
        monkeypatch.setenv(partial, 'test-only')
    monkeypatch.setattr(prospeo, 'find_email', lambda *a, **k: pytest.fail('Missing Prospeo key'))
    monkeypatch.setattr(tomba, 'find_email', lambda *a, **k: pytest.fail('Incomplete Tomba keys'))
    monkeypatch.setattr(hunter, 'api_key', lambda: 'test-only')
    monkeypatch.setattr(hunter, 'make_finder', lambda *a, **k: lambda people: {
        ('cai', 'tech'): {'email': 'cai@beta.io', 'validationStatus': 'valid'}})
    meta = {'beta': {}}
    startup_pipeline.enrich_contacts([ROW], meta, {}, {}, {}, tmp_path, date.today(), search=None,
        progress=lambda m: None, read_site=lambda url: {'pages': [], 'emails': [], 'leaders': [
            {'name': 'Cai Tech', 'title': 'CTO', 'source': 'https://beta.io/team', 'url': ''}]})
    assert meta['beta']['Email Provider'] == 'HUNTER'
    assert not list((tmp_path / 'provider-usage').glob('*.json'))


def test_temporary_refusal_stops_run_but_does_not_cache_no_match_for_week(monkeypatch, tmp_path):
    from datetime import timedelta
    calls, lookups = [], {}
    monkeypatch.setattr(prospeo, 'api_key', lambda: 'test-only')
    def limited(*args, **kwargs):
        calls.append('prospeo')
        raise LimitReached('daily allowance used')
    monkeypatch.setattr(prospeo, 'find_email', limited)
    rows = [ROW, {**ROW, 'Company': 'Gamma', 'Domain': 'gamma.io', 'Website': 'https://gamma.io'}]
    site = lambda url: {'pages': [url], 'emails': [], 'leaders': [
        {'name': 'Cai Tech', 'title': 'CTO', 'source': url, 'url': url}]}
    for day in [date.today(), date.today() + timedelta(days=1)]:
        meta = {'beta': {}, 'gamma': {}}
        startup_pipeline.enrich_contacts(rows, meta, {}, lookups, {}, tmp_path, day,
            search=None, progress=lambda m: None, read_site=site)
        assert all(r['Email Provider'] == 'NO_VERIFIED_EMAIL' for r in meta.values())
        assert lookups['beta.io']['retry_needed'] and lookups['gamma.io']['retry_needed']
    assert calls == ['prospeo', 'prospeo']  # once per run, retried after daily reset


def test_outside_the_size_range_spends_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(prospeo, "find_email", lambda *a, **k: calls.append("prospeo"))
    metadata = {"beta": {}}
    startup_pipeline.enrich_contacts([ROW], metadata, {"beta": {"low": 500, "high": 500}}, {}, {}, tmp_path,
                                     date(2026, 10, 2), search=None, progress=lambda m: None,
                                     read_site=lambda url: calls.append("site"))
    assert calls == [] and "Email Provider" not in metadata["beta"]
