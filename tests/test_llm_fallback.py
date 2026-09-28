from types import SimpleNamespace

import pytest

from jobagent.llm import GeminiProvider, ProviderError


class Busy(Exception):
    pass


Busy.__name__ = "InternalServerError"


class Gone(Exception):
    pass


Gone.__name__ = "NotFoundError"


def fake_client(outcomes, models):
    calls = []

    def create(model, **kwargs):
        calls.append(model)
        outcome = outcomes.get(model, "ok")
        if isinstance(outcome, type) and issubclass(outcome, Exception):
            raise outcome(model)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=f"answer from {model}"))])

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        models=SimpleNamespace(list=lambda: [SimpleNamespace(id=f"models/{m}") for m in models]))
    return client, calls


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    return GeminiProvider({"model": "gemini-flash-latest"})


def test_busy_model_falls_back_to_an_available_one(provider):
    available = ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-3.8-flash",
                 "gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-3.1-flash-image", "gemini-3.8-flash-tts"]
    provider.client, calls = fake_client({"gemini-flash-latest": Busy, "gemini-flash-lite-latest": Busy,
                                          "gemini-3.5-flash-lite": Gone}, available)
    assert provider.text("s", "p") == "answer from gemini-3.8-flash"
    # Aliases, then Lite, then full Flash; image/tts models are never tried.
    assert calls == ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-3.8-flash"]
    calls.clear()
    # Busy models move to the back and retired ones are dropped for the run.
    assert provider.text("s", "p") == "answer from gemini-3.8-flash"
    assert calls == ["gemini-3.8-flash"]


def test_all_models_failing_raises(provider):
    provider.client, _ = fake_client({"gemini-flash-latest": Busy, "gemini-3.8-flash": Gone},
                                     ["gemini-flash-latest", "gemini-3.8-flash"])
    with pytest.raises(ProviderError):
        provider.text("s", "p")


def test_discovery_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    provider = GeminiProvider({"model": "gemini-flash-latest", "discover_fallbacks": False})
    provider.client, calls = fake_client({"gemini-flash-latest": Busy}, ["gemini-3.8-flash"])
    with pytest.raises(ProviderError):
        provider.text("s", "p")
    assert calls == ["gemini-flash-latest"]
