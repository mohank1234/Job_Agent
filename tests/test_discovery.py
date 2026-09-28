from datetime import date
from pathlib import Path

from jobagent import discovery
from jobagent.profile import load_profile

PROFILE = load_profile(Path(__file__).resolve().parents[1] / "profile.yaml.example")


def test_job_queries_come_from_profile_and_change_daily():
    day1 = discovery.daily_job_queries(PROFILE, date(2026, 9, 28), 6)
    assert day1 == discovery.daily_job_queries(PROFILE, date(2026, 9, 28), 6)
    assert day1 != discovery.daily_job_queries(PROFILE, date(2026, 9, 29), 6)
    roles, _, _ = discovery.profile_terms(PROFILE)
    assert len(day1) == 6 and all(any(q.startswith(r + " ") for r in roles) for q in day1)


def test_growth_queries_rotate_and_name_the_month():
    queries = discovery.daily_growth_queries(PROFILE, date(2026, 9, 28), 3)
    assert len(queries) == 3 and all("September 2026" in q for q in queries)
    assert queries != discovery.daily_growth_queries(PROFILE, date(2026, 10, 5), 3)


def test_daily_subset_walks_the_whole_pool():
    pool = [f"term {i}" for i in range(12)]
    seen = set()
    for offset in range(3):
        seen.update(discovery.daily_subset(pool, date(2026, 9, 28 + offset), 4))
    assert seen == set(pool)


def test_slug_candidates():
    assert discovery.slug_candidates("Human Archive") == ["humanarchive", "human-archive"]
    assert "higgsfield" in discovery.slug_candidates("Higgsfield AI")
    # A short core word is too likely to be some other company's board.
    assert discovery.slug_candidates("Norm Law") == ["normlaw", "norm-law"]


def test_extract_companies_rejects_names_not_in_the_source():
    class Provider:
        def structured(self, system, prompt, model):
            return model(companies=[{"name": "Gamma Robotics", "evidence": "x"},
                                    {"name": "Invented Labs", "evidence": "x"}])
    results = [{"url": "https://news.example/a", "title": "Funding news", "snippet": "Gamma Robotics raised $5M."}]
    assert discovery.extract_companies(Provider(), results) == ["Gamma Robotics"]
    assert discovery.extract_companies(None, results) == []


def test_find_new_boards_looks_each_name_up_once(monkeypatch):
    probed = []
    monkeypatch.setattr(discovery, "probe_board",
                        lambda name, client: probed.append(name) or ("https://jobs.ashbyhq.com/gamma" if name == "Gamma" else None))

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    known = {"delta": {"name": "Delta", "checked": "2026-09-01", "board": ""}}
    found = discovery.find_new_boards(["Gamma", "Delta", "Epsilon"], known, client_factory=Client)
    assert found == ["https://jobs.ashbyhq.com/gamma"]
    assert probed == ["Gamma", "Epsilon"]
    assert known["epsilon"]["board"] == "" and known["gamma"]["board"]
    assert discovery.find_new_boards(["Gamma", "Epsilon"], known, client_factory=Client) == []


def test_search_board_requires_matching_slug():
    results = [{"url": "https://jobs.ashbyhq.com/unrelated/1"}, {"url": "https://jobs.lever.co/gammarobotics/2"}]
    assert discovery.search_board("Gamma Robotics", lambda *a, **k: results) == "https://jobs.lever.co/gammarobotics/2"
    assert discovery.search_board("Gamma Robotics", lambda *a, **k: results[:1]) is None
