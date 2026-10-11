import httpx
import pytest

from jobagent.sources import yc_directory as yc


def company(name="Acme", **changes):
    return {"name": name, "website": f"https://www.{name.lower()}.io", "team_size": 50,
            "isHiring": True, "status": "Active", "one_liner": "Software for teams",
            "url": f"https://www.ycombinator.com/companies/{name.lower()}", **changes}


def test_fetch_reports_http_failure_and_keeps_caller_client_open():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(429, json=[]))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            yc.fetch_hiring(client)
        assert not client.is_closed


def test_fetch_rejects_non_directory_response():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as client:
        with pytest.raises(ValueError, match="not a list"):
            yc.fetch_hiring(client)


@pytest.mark.parametrize("website, expected", [
    ("https://www.acme.io/careers?from=yc", "acme.io"),
    ("acme.io", "acme.io"),
    ("//www.acme.io/", "acme.io"),
    ("https://careers.acme.io", "careers.acme.io"),
    ("mailto:founder@acme.io", ""),
    ("https://name:password@acme.io", ""),
    ("https://127.0.0.1", ""),
    ("https://www.linkedin.com/company/acme", ""),
    ("https://[", ""),
    (None, ""),
])
def test_only_normalized_website_domains_are_used(website, expected):
    assert yc.domain_of(website) == expected


def test_candidate_filter_retains_general_hiring_without_claiming_qa_opening():
    rows = yc.candidates([
        company(), company("Tiny", team_size=9), company("Large", team_size=201),
        company("Inactive", status="Inactive"), company("Paused", isHiring=False),
        company("NoDomain", website=""), {**company("NoName"), "name": ""},
        company("BadSize", team_size=True), company("StringFlag", isHiring="false"), None,
    ])
    assert [r["name"] for r in rows] == ["Acme"]
    assert rows[0]["is_hiring"] is True
    assert "has_relevant_opening" not in rows[0]
    assert rows[0]["source"] == "Y Combinator directory"


def test_candidate_ranking_identity_and_location_are_preserved():
    rows = yc.candidates([
        company("US", regions=["United States"], one_liner="AI software"),
        company("India", regions=["India", "Remote"], one_liner="AI testing software", team_size=35,
                all_locations="Bengaluru, India; Remote"),
        company("India duplicate", website="http://india.io/"),
        company("Malformed", tags=[None, "SaaS"], regions=None, industries=[3], one_liner=None),
    ], primary=["India"], places=["Remote"])
    assert rows[0]["name"] == "India"
    assert rows[0]["domain"] == "india.io"
    assert rows[0]["team_size"] == 35
    assert rows[0]["location"] == "Bengaluru, India; Remote"
    assert sum(r["domain"] == "india.io" for r in rows) == 1


def test_bare_website_gets_a_usable_public_url():
    assert yc.candidates([company(website="www.acme.io")])[0]["website"] == "https://www.acme.io"
