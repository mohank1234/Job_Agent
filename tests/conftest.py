import pytest


@pytest.fixture(autouse=True)
def offline_sources(monkeypatch):
    """No test reaches the live Hacker News API, and the web-search budget
    one daily-run test configures never leaks into another test."""
    from jobagent.enrich import exa
    from jobagent.sources import hn_hiring
    monkeypatch.setattr(hn_hiring, "latest_posts", lambda *a, **k: [])
    yield
    exa.configure_budget(None, None)
