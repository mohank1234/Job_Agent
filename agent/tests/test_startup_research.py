from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from jobagent.profile import load_profile
from jobagent.startup_research import check_openings
from tests.test_regressions import posting

PROFILE = load_profile(Path(__file__).resolve().parents[1] / "profile.yaml.example")
PROFILE.raw["identity"]["experience"]["years"] = 5
COMPANY = {"name": "Beta", "slug": "beta", "website": "https://beta.io", "domain": "beta.io"}


class Client:
    def close(self):
        pass


def qa_job():
    job = replace(posting(), company="beta", url="https://jobs.ashbyhq.com/beta/1")
    job.description += " Qualifications: 5 years of QA experience."
    return job


def test_board_with_a_qa_role_is_an_opening():
    result = check_openings(COMPANY, PROFILE, client=Client(),
                            probe=lambda name, client: "https://jobs.ashbyhq.com/beta",
                            fetch=lambda url, client: ([qa_job()], "https://api.ashbyhq.com/posting-api/job-board/beta"))
    assert result["has_relevant_opening"] and result["complete"]
    assert result["rows"][0]["Job Link"] == "https://jobs.ashbyhq.com/beta/1"
    assert result["coverage"][0]["Board URL"] == "https://jobs.ashbyhq.com/beta"


def test_board_without_qa_roles_is_checked_with_no_opening():
    other = replace(qa_job(), title="Backend Engineer")
    result = check_openings(COMPANY, PROFILE, client=Client(), probe=lambda name, client: "https://jobs.lever.co/beta",
                            fetch=lambda url, client: ([other], "https://api.lever.co/v0/postings/beta"))
    assert not result["has_relevant_opening"] and result["complete"]
    assert "No relevant QA opening" in result["status"]


def test_website_link_to_a_board_is_followed():
    pages = {"https://beta.io": '<a href="https://job-boards.greenhouse.io/betainc/jobs/9">Careers</a>'}
    seen = []
    result = check_openings(COMPANY, PROFILE, client=Client(), probe=lambda name, client: None,
                            get=lambda client, url: SimpleNamespace(text=pages.get(url, "")),
                            fetch=lambda url, client: seen.append(url) or ([], url))
    assert seen == ["https://job-boards.greenhouse.io/betainc"]
    assert result["complete"] and not result["has_relevant_opening"]


def test_careers_page_mention_is_reported_but_not_claimed_as_an_opening():
    pages = {"https://beta.io/careers": "<h3>Senior QA Automation Engineer</h3><p>Bengaluru</p>"}
    result = check_openings(COMPANY, PROFILE, client=Client(), probe=lambda name, client: None,
                            get=lambda client, url: SimpleNamespace(text=pages.get(url, "<html></html>")))
    assert not result["has_relevant_opening"] and result["complete"]
    assert "careers page mentions 'Senior QA Automation Engineer'" in result["status"]


def test_rows_verified_today_are_reused_without_new_requests():
    row = {"Company": "beta", "Fit Status": "in_scope", "Job Link": "https://jobs.ashbyhq.com/beta/1"}
    fail = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network expected"))
    result = check_openings(COMPANY, PROFILE, existing_rows=[row], probe=fail, fetch=fail, get=fail)
    assert result["has_relevant_opening"] and result["rows"] == [row]
