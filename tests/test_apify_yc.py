"""Apify Work at a Startup adapter: fetch errors surface distinctly, and
scored rows dedup correctly (the applyUrl Apify returns is NOT unique per
job - every posting shares the same account.ycombinator.com/authenticate
path, only differing by a query param canonical() strips - so Job Link
must be built from the job's own id instead)."""
from pathlib import Path

import httpx
import pytest

from jobagent.enrich import apify_yc
from jobagent.enrich._errors import AdapterError
from jobagent.morning import assess_apify_leads
from jobagent.profile import load_profile


@pytest.fixture
def profile():
    p = load_profile(Path(__file__).resolve().parents[1] / "profile.yaml.example")
    p.raw["identity"]["experience"]["years"] = 5
    p.raw["preferences"].update(home_country="India", onsite_cities=[["Hyderabad"]], qa_roles_only=True)
    return p


def yc_item(**changes):
    item = {
        "id": "55001", "title": "Senior QA Engineer", "company": "Testcorp",
        "location": "Remote (India)", "isRemote": True, "salary": "$80K - $120K",
        "companyWebsite": "https://testcorp.example",
        "description": ("Build automated regression tests with Playwright and Python. " * 10)
                        + "Requires 5+ years of QA automation experience.",
    }
    item.update(changes)
    return item


def test_fetch_yc_jobs_requires_api_key(monkeypatch):
    monkeypatch.delenv("APIFY_API_KEY", raising=False)
    with pytest.raises(AdapterError) as exc:
        apify_yc.fetch_yc_jobs(["QA"])
    assert exc.value.kind == "unauthorized"


def test_fetch_yc_jobs_classifies_http_errors(monkeypatch):
    monkeypatch.setenv("APIFY_API_KEY", "fake-token")

    def raise_401(*args, **kwargs):
        request = httpx.Request("POST", "https://api.apify.com/v2/acts/x/run-sync-get-dataset-items")
        response = httpx.Response(401, request=request, text="Invalid token")
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(httpx, "post", raise_401)
    with pytest.raises(AdapterError) as exc:
        apify_yc.fetch_yc_jobs(["QA"])
    assert exc.value.kind == "unauthorized"


def test_fetch_yc_jobs_rejects_non_list_response(monkeypatch):
    monkeypatch.setenv("APIFY_API_KEY", "fake-token")

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "not a list"}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())
    with pytest.raises(AdapterError) as exc:
        apify_yc.fetch_yc_jobs(["QA"])
    assert exc.value.kind == "invalid_response"


def test_apify_rows_dedup_by_job_id_not_shared_apply_url(profile):
    # Two distinct postings whose applyUrl differs only by a query param
    # canonical() strips for unrecognized hosts - Job Link must not collapse them.
    items = [yc_item(id="1", title="QA Engineer A"), yc_item(id="2", title="QA Engineer B")]
    rows = assess_apify_leads(items, profile)
    assert len(rows) == 2
    assert {r["Job Link"] for r in rows} == {
        "https://www.workatastartup.com/jobs/1",
        "https://www.workatastartup.com/jobs/2",
    }


def test_apify_rows_skip_incomplete_items(profile):
    rows = assess_apify_leads([yc_item(description="too short")], profile)
    assert rows == []
    rows = assess_apify_leads([yc_item(id="")], profile)
    assert rows == []


def test_apify_row_matches_scored_fields_shape(profile):
    row = assess_apify_leads([yc_item()], profile)[0]
    assert row["JD Status"] == "verified_live"
    assert row["Listing Type"] == "Y Combinator Work at a Startup (verified live posting)"
    assert row["Company"] == "Testcorp"
    assert row["Fit Status"] in ("in_scope", "needs_review", "out_of_scope")
    assert len(row["JD SHA256"]) == 64
