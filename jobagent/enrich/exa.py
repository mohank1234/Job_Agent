"""Exa search adapter — company, job, and professional-profile discovery.

Public documented API: https://docs.exa.ai/reference/search
Requires EXA_API_KEY (free tier: $20 signup credit + $10/month recurring,
no card needed, checked 2026-09-08 at https://exa.ai/pricing).
"""

from __future__ import annotations

import os

import httpx

from ._errors import AdapterError, classify_http_error

EXA_API_URL = "https://api.exa.ai/search"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

ExaError = AdapterError


def exa_search(
    query: str,
    num_results: int = 10,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_published_date: str | None = None,
    max_characters: int = 500,
) -> list[dict]:
    """Run a neural search query. Returns a list of
    {title, url, published_date, author, snippet}.

    Raises ExaError (AdapterError) with a `.kind` on failure — never returns
    an empty list to mean "something went wrong"; a genuinely empty result
    set and a broken key must not look the same to the caller.
    """
    api_key = os.environ.get("EXA_API_KEY", "")
    if not api_key:
        raise ExaError("unauthorized", "EXA_API_KEY is not set")

    body: dict = {"query": query, "numResults": num_results, "contents": {"text": {"maxCharacters": max_characters}}}
    if include_domains:
        body["includeDomains"] = include_domains
    if exclude_domains:
        body["excludeDomains"] = exclude_domains
    if start_published_date:
        body["startPublishedDate"] = start_published_date

    try:
        resp = httpx.post(
            EXA_API_URL,
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        if isinstance(exc, ExaError):
            raise
        raise classify_http_error(exc, "Exa") from exc

    if "results" not in data:
        raise ExaError("invalid_response", f"Exa response had no 'results' key: {list(data.keys())}")

    out = []
    for r in data["results"]:
        text = r.get("text") or ""
        out.append({
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "published_date": r.get("publishedDate"),
            "author": r.get("author"),
            "snippet": text[:max_characters],
        })
    return out
