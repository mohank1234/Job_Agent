"""Firecrawl adapter — permitted extraction of public websites and job
descriptions into clean markdown, including JS-rendered pages a plain HTTP
GET can't read.

Public documented API: https://docs.firecrawl.dev/api-reference/endpoint/scrape
Requires FIRECRAWL_API_KEY (free tier: 1,000 credits/month, no card, checked
2026-09-08 via firecrawl.dev's pricing page).
"""

from __future__ import annotations

import os

import httpx

from ._errors import AdapterError, classify_http_error

FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)   # JS rendering can take a while

FirecrawlError = AdapterError


def firecrawl_scrape(url: str, formats: list[str] | None = None) -> dict:
    """Fetch one URL and return {markdown, html, metadata, success}.

    Raises FirecrawlError (AdapterError) with a `.kind` on failure. A target
    page that itself 404s or errors is NOT an adapter failure — Firecrawl
    still succeeded at fetching it, so this returns the page's own content
    (e.g. a 404 page's markdown) rather than raising; check
    `result["metadata"].get("statusCode")` if the target page's own status
    matters to the caller.
    """
    api_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not api_key:
        raise FirecrawlError("unauthorized", "FIRECRAWL_API_KEY is not set")

    try:
        resp = httpx.post(
            FIRECRAWL_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": url, "formats": formats or ["markdown"]},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        if isinstance(exc, FirecrawlError):
            raise
        raise classify_http_error(exc, "Firecrawl") from exc

    if not data.get("success"):
        raise FirecrawlError("invalid_response", f"Firecrawl reported failure: {data.get('error', data)}")

    page = data.get("data") or {}
    return {
        "markdown": page.get("markdown", ""),
        "html": page.get("html", ""),
        "metadata": page.get("metadata") or {},
        "success": True,
    }
