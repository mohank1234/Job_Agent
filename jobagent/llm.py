"""Provider abstraction — the rest of the codebase never imports an SDK directly.

Three backends:
  anthropic   — the Anthropic SDK, native structured outputs
  openrouter  — the OpenAI-compatible SDK against openrouter.ai, 400+ models
  omnirate    — local OmniRoute gateway (OpenAI-compatible, free models)

They speak different wire formats (Anthropic uses `output_config.format`,
OpenRouter/OmniRoute use OpenAI's `response_format.json_schema`), so all are
wrapped behind `structured()` / `text()` and selected from config.
"""

from __future__ import annotations

import json
import os
import re
from typing import Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OMNIROUTE_BASE = "http://localhost:20128/v1"
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class ProviderError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a response that may be fenced or prefaced."""
    text = (text or "").strip()
    if not text:
        raise ProviderError("empty response from model")

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise ProviderError(f"no JSON object in response: {text[:200]}")


# ---------------------------------------------------------------- Anthropic

class AnthropicProvider:
    name = "anthropic"

    def __init__(self, cfg: dict):
        import anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ProviderError(
                "ANTHROPIC_API_KEY is not set. Either set it, or switch "
                "config.yaml -> llm.provider to 'openrouter'."
            )
        self.client = anthropic.Anthropic()
        self.model = cfg.get("model", "claude-opus-5")
        self.effort = cfg.get("effort", "high")

    def structured(self, system: str, prompt: str, model_cls: Type[T]) -> T:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            output_config={
                "effort": self.effort,
                "format": {
                    "type": "json_schema",
                    "schema": model_cls.model_json_schema(),
                },
            },
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            raise ProviderError(f"model declined: {response.stop_details}")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return model_cls.model_validate(_extract_json(text))

    def text(self, system: str, prompt: str, max_tokens: int = 2000) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            return "(model declined)"
        return next((b.text for b in response.content if b.type == "text"), "").strip()


# --------------------------------------------------------------- OpenRouter

class OpenRouterProvider:
    """OpenAI-compatible. Note OpenRouter bills Claude models at the same
    per-token rate as Anthropic direct — the saving comes from picking a
    cheaper model, not from the proxy itself."""

    name = "openrouter"

    def __init__(self, cfg: dict):
        from openai import OpenAI

        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise ProviderError(
                "OPENROUTER_API_KEY is not set. Get one at "
                "https://openrouter.ai/keys (format: sk-or-v1-...)"
            )
        self.client = OpenAI(base_url=OPENROUTER_BASE, api_key=key)
        self.model = cfg.get("model") or "anthropic/claude-sonnet-5"
        self.headers = {
            "HTTP-Referer": "https://github.com/local/job-agent",
            "X-Title": "job-agent",
        }

    def _chat(self, system: str, prompt: str, max_tokens: int, response_format=None):
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            extra_headers=self.headers,
        )
        if response_format:
            kwargs["response_format"] = response_format
        resp = self.client.chat.completions.create(**kwargs)
        if not resp.choices:
            err = getattr(resp, "error", None)
            raise ProviderError(f"no choices returned: {err or resp}")
        return resp.choices[0].message.content or ""

    def structured(self, system: str, prompt: str, model_cls: Type[T]) -> T:
        schema = model_cls.model_json_schema()
        fmt = {
            "type": "json_schema",
            "json_schema": {
                "name": model_cls.__name__.lower(),
                "strict": True,
                "schema": schema,
            },
        }
        try:
            text = self._chat(system, prompt, 8000, response_format=fmt)
            return model_cls.model_validate(_extract_json(text))
        except Exception:
            # Not every OpenRouter model supports json_schema. Fall back to
            # asking for JSON in the prompt and parsing what comes back.
            nudge = (
                f"{prompt}\n\n"
                "Reply with ONLY a JSON object matching this schema. "
                "No prose, no markdown fences.\n"
                f"{json.dumps(schema)}"
            )
            text = self._chat(system, nudge, 8000)
            return model_cls.model_validate(_extract_json(text))

    def text(self, system: str, prompt: str, max_tokens: int = 2000) -> str:
        return self._chat(system, prompt, max_tokens).strip()


# --------------------------------------------------------------- OmniRoute

def _is_free_alias(model: str) -> bool:
    """OmniRoute names free routes with a '-free' suffix or an auto/ alias."""
    m = (model or "").lower()
    return "free" in m or m.startswith("auto/offline")


class OmniRouteProvider:
    """Local OmniRoute gateway (OpenAI-compatible). Routes to free models
    by default. No external API key needed — just a running OmniRoute instance."""

    name = "omnirate"

    def __init__(self, cfg: dict):
        from openai import OpenAI

        base_url = cfg.get("base_url") or OMNIROUTE_BASE
        # OmniRoute doesn't require a real API key; any non-empty string works
        api_key = os.environ.get("OMNIROUTE_API_KEY") or "sk-omni"
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=float(cfg.get("timeout_seconds", 180)),
            max_retries=0,          # matcher.py owns retry + exponential backoff
        )
        self.model = cfg.get("model") or "auto/best-free"
        # Free models get rate-limited individually; try the next one rather
        # than failing the batch. All entries must be free.
        self.fallback_models = [
            m for m in (cfg.get("fallback_models") or []) if m and m != self.model
        ]
        # Guardrail: this pipeline is supposed to cost nothing. Refuse to start
        # on a model id that isn't recognisably free rather than discovering it
        # on a bill. Set llm.free_models_only: false to override deliberately.
        trusted = "omnirate" in (cfg.get("free_providers") or [])
        if bool(cfg.get("free_models_only", True)) and not trusted:
            paid = [m for m in [self.model, *self.fallback_models] if not _is_free_alias(m)]
            if paid:
                raise ProviderError(
                    f"llm.free_models_only is on and these model ids are not "
                    f"recognisably free: {paid}. Use a '*-free' id or an "
                    f"'auto/offline' / 'auto/*-free' alias."
                )
        self.max_tokens = int(cfg.get("max_tokens", 4000))
        self.last_model_used = self.model
        # None = not yet known. Free models generally do not implement
        # response_format.json_schema, and asking for it also changes OmniRoute's
        # routing. Probing once and remembering the answer halves the number of
        # calls for the rest of the run instead of paying for a doomed attempt
        # on every batch. Force it either way with llm.use_json_schema.
        self._schema_works: bool | None = cfg.get("use_json_schema")

    def _chat(self, system: str, prompt: str, max_tokens: int, response_format=None):
        errors: list[str] = []
        for model in [self.model, *self.fallback_models]:
            kwargs = dict(
                model=model,
                max_tokens=min(max_tokens, self.max_tokens),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            if response_format:
                kwargs["response_format"] = response_format
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception as exc:                     # rate limit, 5xx, timeout
                errors.append(f"{model}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            if not resp.choices:
                errors.append(f"{model}: no choices ({getattr(resp, 'error', '')})")
                continue
            self.last_model_used = model
            return resp.choices[0].message.content or ""
        raise ProviderError("all free models failed — " + " | ".join(errors[:4]))

    def structured(self, system: str, prompt: str, model_cls: Type[T]) -> T:
        schema = model_cls.model_json_schema()
        # Try native json_schema first (OmniRoute may support it via some providers)
        fmt = {
            "type": "json_schema",
            "json_schema": {
                "name": model_cls.__name__.lower(),
                "strict": True,
                "schema": schema,
            },
        }
        if self._schema_works is not False:
            try:
                text = self._chat(system, prompt, 8000, response_format=fmt)
                parsed = model_cls.model_validate(_extract_json(text))
                self._schema_works = True
                return parsed
            except Exception:
                # Remember the failure: on a free model this costs a full call
                # per batch, and it will fail the same way every time.
                self._schema_works = False

        # Prompt-instructed JSON with the schema inline. Free models emit
        # reasoning prose around the object, which _extract_json handles.
        nudge = (
            f"{prompt}\n\n"
            "Reply with ONLY a JSON object matching this schema. "
            "No prose, no markdown fences, no explanation before or after.\n"
            f"{json.dumps(schema)}"
        )
        text = self._chat(system, nudge, 8000)
        return model_cls.model_validate(_extract_json(text))

    def text(self, system: str, prompt: str, max_tokens: int = 2000) -> str:
        return self._chat(system, prompt, max_tokens).strip()


# --------------------------------------------------------------------- Gemini
#
# Free tier measured 2026-08-20 (aistudio.google.com/apikey, no card):
#   gemini-flash-latest   10 req/min · 250,000 tokens/min · 500-1,500 req/day
# That token-per-minute headroom is ~30x Groq's free tier, which is what this
# workload actually needs: a 4-job batch is ~5-8k tokens.
#
# Compared against the alternatives before choosing:
#   Groq openai/gpt-oss-120b  30 RPM but only 8K tokens/min - throttles a batch
#   Cerebras                  1M tokens/day but an 8,192-token CONTEXT cap,
#                             which cannot hold 4 job descriptions at all
class GeminiProvider:
    """Google Gemini via its OpenAI-compatible endpoint.

    Uses native JSON-schema structured output, so the prompt-nudge fallback
    that the free OmniRoute models needed is not required here.
    """

    name = "gemini"

    def __init__(self, cfg: dict):
        from openai import OpenAI

        api_key = os.environ.get(cfg.get("api_key_env", "GEMINI_API_KEY"), "")
        if not api_key:
            raise ProviderError(
                "GEMINI_API_KEY is not set. Free key, no card, 60 seconds: "
                "https://aistudio.google.com/apikey  then "
                'setx GEMINI_API_KEY "your-key" and reopen the terminal.'
            )
        self.client = OpenAI(
            api_key=api_key,
            base_url=cfg.get("base_url")
            or "https://generativelanguage.googleapis.com/v1beta/openai/",
            timeout=float(cfg.get("timeout_seconds", 120)),
            max_retries=0,           # matcher.py owns retry + backoff
        )
        self.model = cfg.get("model") or "gemini-flash-latest"
        self.fallback_models = [
            m for m in (cfg.get("fallback_models") or []) if m and m != self.model
        ]
        self.max_tokens = int(cfg.get("max_tokens", 8000))
        self.last_model_used = self.model

    def _chat(self, system: str, prompt: str, max_tokens: int, response_format=None):
        errors: list[str] = []
        for model in [self.model, *self.fallback_models]:
            kwargs = dict(
                model=model,
                max_tokens=min(max_tokens, self.max_tokens),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            if response_format:
                kwargs["response_format"] = response_format
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                errors.append(f"{model}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            if not resp.choices:
                errors.append(f"{model}: no choices returned")
                continue
            self.last_model_used = model
            return resp.choices[0].message.content or ""
        raise ProviderError("gemini: " + " | ".join(errors[:3]))

    def structured(self, system: str, prompt: str, model_cls):
        schema = model_cls.model_json_schema()
        fmt = {
            "type": "json_schema",
            "json_schema": {"name": model_cls.__name__.lower(),
                            "strict": True, "schema": schema},
        }
        try:
            text = self._chat(system, prompt, self.max_tokens, response_format=fmt)
            return model_cls.model_validate(_extract_json(text))
        except ProviderError:
            raise
        except Exception:
            nudge = (
                f"{prompt}\n\nReply with ONLY a JSON object matching this schema. "
                f"No prose, no markdown fences.\n{json.dumps(schema)}"
            )
            text = self._chat(system, nudge, self.max_tokens)
            return model_cls.model_validate(_extract_json(text))

    def text(self, system: str, prompt: str, max_tokens: int = 2000) -> str:
        return self._chat(system, prompt, max_tokens).strip()


class GroqProvider(GeminiProvider):
    """Groq. Same OpenAI-compatible shape; different endpoint and key.

    Fastest of the free tiers, but the free plan caps tokens-per-minute at 8K,
    which throttles a 4-job batch. Kept as a fallback, not the default.
    """

    name = "groq"

    def __init__(self, cfg: dict):
        cfg = {
            **cfg,
            "api_key_env": cfg.get("api_key_env", "GROQ_API_KEY"),
            "base_url": cfg.get("base_url") or "https://api.groq.com/openai/v1",
            "model": cfg.get("model") or "openai/gpt-oss-120b",
        }
        try:
            super().__init__(cfg)
        except ProviderError:
            raise ProviderError(
                "GROQ_API_KEY is not set. Free key: https://console.groq.com/keys "
                'then setx GROQ_API_KEY "your-key" and reopen the terminal.'
            )
        self.name = "groq"


PROVIDERS = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "anthropic": AnthropicProvider,
    "openrouter": OpenRouterProvider,
    "omnirate": OmniRouteProvider,
}


def make_provider(cfg: dict):
    name = (cfg.get("provider") or "anthropic").lower()
    # Cost guard: a provider must be on the trusted free list, unless the user
    # deliberately turns the guard off. This is what stops a stray config edit
    # pointing the pipeline at a billed endpoint.
    if bool(cfg.get("free_models_only", True)):
        free_providers = cfg.get("free_providers") or ["omnirate"]
        if name not in free_providers:
            raise ProviderError(
                f"provider '{name}' is not in llm.free_providers "
                f"{free_providers}. It may be a paid endpoint. Add it there "
                f"only if you know its free tier covers this usage."
            )
    if name not in PROVIDERS:
        raise ProviderError(
            f"unknown provider '{name}' — choose one of {list(PROVIDERS)}"
        )
    return PROVIDERS[name](cfg)


def list_openrouter_models(search: str | None = None, free_only: bool = False):
    """Public endpoint — no API key needed. Used by `run.py models`."""
    import httpx

    resp = httpx.get(f"{OPENROUTER_BASE}/models", timeout=30)
    resp.raise_for_status()
    rows = []
    for m in resp.json().get("data", []):
        pricing = m.get("pricing") or {}
        try:
            pin = float(pricing.get("prompt") or 0) * 1e6
            pout = float(pricing.get("completion") or 0) * 1e6
        except (TypeError, ValueError):
            pin = pout = 0.0
        # OpenRouter uses -1 to mean "variable / routed pricing" (openrouter/auto).
        # Treat it as unknown rather than free, or it sorts to the top of --free.
        if pin < 0 or pout < 0:
            pin = pout = float("nan")
        if free_only and not (pin == 0 and pout == 0):
            continue
        if search and search.lower() not in m.get("id", "").lower():
            continue
        rows.append(
            {
                "id": m.get("id", ""),
                "in": pin,
                "out": pout,
                "ctx": m.get("context_length") or 0,
                "structured": "structured_outputs"
                in (m.get("supported_parameters") or []),
            }
        )
    rows.sort(key=lambda r: (r["in"] if r["in"] == r["in"] else 1e9, r["id"]))
    return rows
