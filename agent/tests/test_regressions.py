"""Offline regression tests. All databases, ledgers and services are disposable."""
from __future__ import annotations

import copy
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from jobagent.models import Job
from jobagent.profile import Profile, load_profile
from jobagent.matcher import policy_version, rule_match, match_batch
from jobagent.roles import classify
from jobagent.store import Store
from jobagent.runtime import now_iso, process_lock
from jobagent.outreach.service import draft_rows, parse_rows, validate_row
from jobagent.outreach import verification as verify

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def profile():
    p = load_profile(ROOT / "profile.yaml.example")
    p.raw["identity"]["experience"]["years"] = 5
    p.raw["preferences"].update(home_country="India", onsite_cities=[["Hyderabad"]], qa_roles_only=True)
    return p


def posting(number=1, days=0):
    return Job(source="ashby", company="Example", title="QA Automation Engineer",
               url=f"https://jobs.ashbyhq.com/example/{number}", location="India",
               workplace="remote", description="Develop automated software regression tests with Python and Playwright. " * 8,
               posted_at=datetime.now(timezone.utc) - timedelta(days=days))


def classified(profile, number=1, days=0):
    job = posting(number, days)
    job.apply_classification(classify(job, profile))
    rule_match(job, profile)
    return job


def outreach_row():
    return {"Company": "Example", "Email": "qa@example.com", "Email Subject": "QA role",
            "Email Body": "I have experience testing APIs.", "JD Text": "Actual posting text. " * 20,
            "JD Status": "verified_live", "JD Verified At": now_iso(),
            "Job Link": "https://jobs.ashbyhq.com/example/1", "Contact Verification": "public_email_found",
            "Contact Verified At": now_iso(), "Contact Source URL": "https://example.com/careers",
            "Content Status": "reviewed", "Fit Status": "in_scope"}


def test_full_content_and_classification_invalidate_hash():
    job = posting()
    job.description = "a" * 6000
    original = job.content_hash("policy")
    for name, value in (("description", "a" * 5999 + "b"), ("salary", "100"),
                        ("base_score", 91), ("evidence", {"qa": ["test"]})):
        changed = copy.deepcopy(job)
        setattr(changed, name, value)
        assert changed.content_hash("policy") != original


def test_escaped_greenhouse_html_is_readable():
    from jobagent.models import clean_html
    assert clean_html("&lt;p&gt;Requirements: Python &amp;amp; SQL&lt;/p&gt;") == "Requirements: Python & SQL"


def test_policy_hash_covers_blend_and_actual_prompt(profile, monkeypatch):
    import jobagent.matcher as matcher
    first = policy_version(profile, {"llm_weight": .75})
    assert first != policy_version(profile, {"llm_weight": .5})
    monkeypatch.setattr(matcher, "SYSTEM", matcher.SYSTEM + " extra policy")
    assert first != policy_version(profile, {"llm_weight": .75})


def test_search_countries_are_not_home():
    p = Profile({"preferences": {"home_country": "India", "locations": ["USA", "UK"]}})
    assert "based in India" in p.geography_rule()
    assert "USA" not in p.geography_rule()


def test_incomplete_model_batch_does_not_partially_apply(profile):
    jobs = [posting(1), posting(2)]
    provider = SimpleNamespace(structured=lambda *a: SimpleNamespace(matches=[]))
    with pytest.raises(ValueError, match="every job index"):
        match_batch(provider, jobs, profile, .75)
    assert all(j.scored_by != "llm" for j in jobs)


def test_rules_never_retain_llm_attribution(profile):
    job = classified(profile)
    job.scored_by = "llm"
    rule_match(job, profile)
    assert job.scored_by == "rules"


def test_digest_filters_age_before_limit(tmp_path, profile):
    store = Store(tmp_path / "test.db")
    old = [classified(profile, n, 30) for n in range(150)]
    fresh = classified(profile, 999)
    for job in old + [fresh]:
        job.match_category, job.score = "A", 100 if job is not fresh else 75
    store.upsert_all(old + [fresh])
    rows = store.top(limit=1, max_age_days=7)
    assert [r["url"] for r in rows] == [fresh.url]
    store.close()


def test_backlog_includes_stale_policy_but_excludes_old_postings(tmp_path, profile):
    from jobagent.selection import pending_jobs
    store = Store(tmp_path / "test.db")
    jobs = [classified(profile, 1), classified(profile, 2, 90)]
    store.upsert_all(jobs, policy_version="old-policy")
    for job in jobs:
        job.scored_by = "llm"
        store.record_match(job)
    pending = pending_jobs(store.conn.execute("SELECT * FROM seen"), profile, {}, 7)
    assert [j.url for j in pending] == [jobs[0].url]
    store.close()


def test_score_selection_persists_policy_demotions(tmp_path, profile):
    from jobagent.selection import pending_jobs
    store = Store(tmp_path / "test.db")
    job = posting()
    job.title = "Senior Frontend Developer"
    job.description = "Build React user interfaces and frontend application features."
    job.score, job.match_category, job.scored_by, job.candidate = 99, "A", "llm", True
    store.upsert_all([job], policy_version="old")
    pending_jobs(store.conn.execute("SELECT * FROM seen").fetchall(), profile, {}, 7, store=store)
    row = store.conn.execute("SELECT score,match_category,scored_by FROM seen").fetchone()
    assert row["match_category"] not in ("A", "B", "C") and row["scored_by"] == "rules"
    store.close()


def test_cache_survives_no_llm_fetch(tmp_path, profile, monkeypatch):
    import run
    job = classified(profile)
    store = Store(tmp_path / "jobs.db")
    pv = policy_version(profile, {})
    store.upsert_all([job], policy_version=pv)
    job.score, job.scored_by = 97, "llm"
    store.record_match(job)
    store.close()
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "load_profile", lambda *a: profile)
    monkeypatch.setattr(run, "load_yaml", lambda *a: {"llm": {}})
    monkeypatch.setattr(run, "collect", lambda *a: ([posting()], [], {"ats": 1, "aggregator": 0}))
    # Use the same posting timestamp: changing the source date changes content.
    raw = copy.deepcopy(job)
    monkeypatch.setattr(run, "collect", lambda *a: ([raw], [], {"ats": 1, "aggregator": 0}))
    monkeypatch.setattr(run, "_write_digest", lambda *a, **k: None)
    run.cmd_fetch(SimpleNamespace(no_llm=True, limit=0))
    store = Store(tmp_path / "jobs.db")
    row = store.conn.execute("SELECT score,scored_by FROM seen").fetchone()
    assert tuple(row) == (97, "llm")
    store.close()


def test_draft_checkpoint_survives_second_failure_and_rerun(tmp_path):
    rows = [outreach_row(), {**outreach_row(), "Email": "second@example.com"}]
    calls = []
    def create(**kw):
        calls.append(kw)
        if len(calls) == 2:
            raise TimeoutError()
        return {"draft_id": "real-draft-id"}
    ledger = tmp_path / "ledger.json"
    result = draft_rows(rows, ledger, create)
    assert result["created"] == 1 and result["uncertain"] == 1
    saved = json.loads(ledger.read_text())
    assert {v["state"] for v in saved.values()} == {"drafted", "uncertain"}
    assert draft_rows(rows, ledger, create)["created"] == 0
    assert len(calls) == 2


def test_dry_run_writes_nothing_and_normalizes_recipient(tmp_path):
    rows = [outreach_row(), {**outreach_row(), "Company": "Renamed", "Email": "QA@EXAMPLE.COM"}]
    result = draft_rows(rows, tmp_path / "ledger.json", None, dry_run=True)
    assert result["planned"] == 1 and result["created"] == 0
    assert not list(tmp_path.iterdir())


def test_legacy_ledger_blocks_same_inbox_after_row_rename(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"8|OLD|Name": {"email": "QA@EXAMPLE.COM", "draft_id": "old"}}))
    result = draft_rows([outreach_row()], ledger, None, dry_run=True)
    assert result["planned"] == 0


def test_process_lock_rejects_concurrent_work(tmp_path):
    path = tmp_path / "run.lock"
    with process_lock(path):
        with pytest.raises(RuntimeError, match="Another process"):
            with process_lock(path):
                pytest.fail("Concurrent lock was acquired")
    with process_lock(path):
        pass


@pytest.mark.parametrize("field,value", [("JD Status", "blocked"), ("Contact Verification", "guessed"),
    ("Content Status", "needs_review"), ("Fit Status", "out_of_scope"),
    ("Email", "QA <qa@example.com>"), ("JD Verified At", "2020-01-01T00:00:00Z")])
def test_unverified_content_cannot_be_drafted(field, value):
    row = outreach_row()
    row[field] = value
    assert validate_row(row) is not None


def test_invalid_sheet_schema_fails():
    with pytest.raises(ValueError, match="schema"):
        parse_rows("Company,JD Text\nExample,N/A\n")


def test_sheet_name_collision_does_not_choose_newest(monkeypatch):
    import googleapiclient.discovery
    from jobagent.outreach.gmail import read_sheet_as_csv, GmailAuthError
    service = MagicMock()
    service.files().list().execute.side_effect = [
        {"files": [{"id": "1"}], "nextPageToken": "next"}, {"files": [{"id": "2"}]}]
    monkeypatch.setattr(googleapiclient.discovery, "build", lambda *a, **k: service)
    with pytest.raises(GmailAuthError, match="found 2"):
        read_sheet_as_csv("Report", creds=object())
    service.files().export.assert_not_called()


def test_unattended_auth_never_opens_consent(tmp_path, monkeypatch):
    from jobagent.outreach import gmail
    monkeypatch.setattr(gmail, "TOKEN_PATH", tmp_path / "missing-token.json")
    with pytest.raises(gmail.GmailAuthError, match="interactively"):
        gmail.authenticate()


def test_verification_keeps_real_jd_and_flags_conflicting_workplace(profile, monkeypatch):
    job = posting()
    job.raw = {"isRemote": True, "workplaceType": "Hybrid"}
    monkeypatch.setattr(verify, "fetch_board", lambda *a: ([job], "https://api.ashbyhq.com/test"))
    row = verify.verify_row({"Company": "Example", "Job Link": job.url, "JD Text": "Old unverified claim"}, profile, None)
    assert row["JD Status"] == "verified_live" and row["JD Text"] == job.description
    assert row["Previous JD Claim"] == "Old unverified claim"
    assert row["Fit Status"] == "needs_review"


def test_removed_posting_does_not_reuse_old_jd(profile, monkeypatch):
    monkeypatch.setattr(verify, "fetch_board", lambda *a: ([], "https://api.ashbyhq.com/test"))
    row = verify.verify_row({"Company": "Example", "Job Link": posting().url, "JD Text": "Old claim"}, profile, None)
    assert row["JD Status"] == "not_currently_listed" and row["JD Text"] == ""


def test_blocked_posting_is_not_reported_closed(profile, monkeypatch):
    def blocked(*args):
        response = httpx.Response(403, request=httpx.Request("GET", posting().url))
        response.raise_for_status()
    monkeypatch.setattr(verify, "fetch_board", blocked)
    row = verify.verify_row({"Company": "Example", "Job Link": posting().url}, profile, None)
    assert row["JD Status"] == "blocked"


def test_partial_feed_preserves_jobs_and_reports_error(monkeypatch):
    from jobagent.sources import feeds
    monkeypatch.setattr(feeds, "_get", lambda client, url, **kw: {"jobs": []} if "search=" not in url else (_ for _ in ()).throw(ValueError("bad response")))
    outcomes = []
    jobs, errors = feeds.fetch_feeds({"remotive": {"search_terms": ["qa"]}}, outcomes)
    assert errors and next(o for o in outcomes if o["source"] == "remotive")["status"] == "error"


def test_unsupported_vendor_is_visible():
    from jobagent.sources.ats import fetch_ats
    outcomes = []
    jobs, errors = fetch_ats({"unsupported": ["example"]}, outcomes)
    assert not jobs and "unsupported vendor" in errors[0]
    assert outcomes[0]["status"] == "error"


def test_kit_identity_and_quota_after_existing_filter(tmp_path, monkeypatch):
    import sys
    base = SimpleNamespace(SKILLS=[], EXPERIENCE=[], TAGLINE="QA", NAME="Test",
                           __file__=str(ROOT / "resume/build_resume.py"))
    monkeypatch.setitem(sys.modules, "build_resume", base)
    spec = importlib.util.spec_from_file_location("kit_test_module", ROOT / "resume/tailor.py")
    tailor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tailor)
    first = {"url": "https://example.com/1", "title": "QA", "company": "Same"}
    second = {**first, "url": "https://example.com/2"}
    assert tailor.kit_slug("Same", "QA", first["url"]) != tailor.kit_slug("Same", "QA", second["url"])
    folder = tmp_path / "day" / "kit"
    folder.mkdir(parents=True)
    (folder / "kit.json").write_text(json.dumps({"url": first["url"], "version": tailor.kit_version(first)}))
    assert tailor.select_kits([first, second], tmp_path, 1) == [second]
    changed = {**first, "description": "New requirements"}
    assert tailor.select_kits([changed], tmp_path, 1) == [changed]


def test_evaluation_transaction_rolls_back_on_match_failure(tmp_path, profile, monkeypatch):
    store = Store(tmp_path / "test.db")
    job = classified(profile)
    store.upsert_all([job], policy_version="old")
    old_hash = store.conn.execute("SELECT content_hash FROM seen").fetchone()[0]
    monkeypatch.setattr(store, "record_match", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        store.record_evaluation(job, "new")
    assert store.conn.execute("SELECT content_hash FROM seen").fetchone()[0] == old_hash
    store.close()


def test_outcomes_distinguish_drafts_sends_automatic_and_incoming():
    from jobagent.outreach.outcomes import summarize_threads
    def message(mid, labels, sender, recipient, auto="no"):
        return {"id": mid, "labelIds": labels, "payload": {"headers": [
            {"name": "From", "value": sender}, {"name": "To", "value": recipient},
            {"name": "Auto-Submitted", "value": auto}]}}
    messages = [message("d", ["DRAFT"], "me@example.com", "qa@example.org"),
                message("s", ["SENT"], "me@example.com", "qa@example.org"),
                message("a", ["INBOX"], "qa@example.org", "me@example.com", "auto-replied"),
                message("r", ["INBOX"], "qa@example.org", "me@example.com")]
    result = summarize_threads([{"messages": messages}], "me@example.com", "qa@example.org")
    assert result["sent_message_ids"] == ["s"]
    assert result["incoming_message_ids"] == ["r"]
    assert result["automatic_response_ids"] == ["a"]


def test_workable_valid_empty_board_is_not_schema_error():
    import asyncio
    from jobagent.sources.ats import _fetch_board
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"jobs": []}))) as client:
            return await _fetch_board(client, "workable", "example")
    assert asyncio.run(check())[3] is None


def test_mailbox_failure_exit_code(monkeypatch):
    import run
    import tests.mailbox_cases
    monkeypatch.setattr(tests.mailbox_cases, "run_cases", lambda: [{"case": "broken", "got": 0, "expected": 1, "urls_clean": True, "ok": False}])
    assert run.cmd_mailbox_test(SimpleNamespace(offline=True)) == 1
