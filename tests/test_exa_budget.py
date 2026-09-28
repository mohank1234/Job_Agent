import json

import pytest

from jobagent.enrich import exa


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    monkeypatch.setenv("EXA_API_KEY", "test")
    monkeypatch.setattr(exa.httpx, "post", lambda *a, **k: calls.append(k["json"]) or Response())
    yield calls
    exa.configure_budget(None, None)


def test_searches_stop_before_the_free_credit_is_used(tmp_path, no_network):
    import calendar
    from datetime import datetime, timezone
    path = tmp_path / "exa-usage.json"
    now = datetime.now(timezone.utc)
    path.write_text(json.dumps({"month": now.strftime("%Y-%m"), "spent_usd": 0.0, "searches": 0}))
    days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
    # Allowance for today is what is left of the month, spread over the days left.
    exa.configure_budget(path, 0.2 * days_left)
    done = 0
    with pytest.raises(exa.ExaError) as err:
        for _ in range(100):
            exa.exa_search("q", 10)
            done += 1
    assert err.value.kind == "free_budget_reached"
    spent = json.loads(path.read_text())["spent_usd"]
    assert spent <= 0.2 and len(no_network) == done
    assert done == int(0.2 // exa.search_cost(10))


def test_first_month_with_unknown_earlier_spending_makes_no_searches(tmp_path, no_network):
    path = tmp_path / "exa-usage.json"
    exa.configure_budget(path, 8.0)
    with pytest.raises(exa.ExaError):
        exa.exa_search("q", 10)
    assert no_network == [] and json.loads(path.read_text())["prior_usage_unknown"]
    # A new month starts clean.
    data = json.loads(path.read_text())
    data["month"] = "1999-01"
    path.write_text(json.dumps(data))
    exa.exa_search("q", 10)
    assert len(no_network) == 1


def test_no_budget_means_no_limit(no_network):
    exa.configure_budget(None, None)
    for _ in range(5):
        exa.exa_search("q", 10)
    assert len(no_network) == 5
