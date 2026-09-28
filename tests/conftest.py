import pytest


@pytest.fixture(autouse=True)
def offline_sources(monkeypatch, request):
    """No test reaches the live Hacker News API, and the web-search budget
    one daily-run test configures never leaks into another test."""
    from jobagent.enrich import exa
    from jobagent.sources import hn_hiring, yc_directory
    monkeypatch.setattr(hn_hiring, "latest_posts", lambda *a, **k: [])
    if request.module.__name__ != "tests.test_yc_directory":  # those tests use a mock transport
        monkeypatch.setattr(yc_directory, "fetch_hiring", lambda *a, **k: [])
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    yield
    exa.configure_budget(None, None)
