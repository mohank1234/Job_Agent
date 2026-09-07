"""Shared error classification for external API adapters.

Per the design requirement to distinguish failure types instead of collapsing
everything into "no results": a 401 means the key is wrong, a 429 means slow
down, a 5xx means the provider is having a bad day, and a timeout means
try again later. Treating all of these as "zero results" is how a broken key
looks identical to an empty search forever.
"""

from __future__ import annotations

import httpx


class AdapterError(Exception):
    """Base for all enrichment-adapter errors. `.kind` is one of:
    unauthorized | forbidden | rate_limited | server_error | timeout |
    connection_error | invalid_response | unknown
    """

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(f"[{kind}] {message}")


def classify_http_error(exc: Exception, provider: str) -> AdapterError:
    if isinstance(exc, httpx.TimeoutException):
        return AdapterError("timeout", f"{provider} request timed out: {exc}")
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        body = exc.response.text[:300]
        if code == 401:
            return AdapterError("unauthorized", f"{provider} rejected the API key (401): {body}")
        if code == 403:
            return AdapterError("forbidden", f"{provider} denied access (403): {body}")
        if code == 429:
            return AdapterError("rate_limited", f"{provider} rate-limited this request (429): {body}")
        if 500 <= code < 600:
            return AdapterError("server_error", f"{provider} server error ({code}): {body}")
        return AdapterError("unknown", f"{provider} HTTP {code}: {body}")
    if isinstance(exc, httpx.ConnectError):
        return AdapterError("connection_error", f"{provider} connection failed: {exc}")
    if isinstance(exc, httpx.HTTPError):
        return AdapterError("connection_error", f"{provider} network error: {exc}")
    if isinstance(exc, (ValueError, KeyError)):
        return AdapterError("invalid_response", f"{provider} returned an unexpected response shape: {exc}")
    return AdapterError("unknown", f"{provider}: {type(exc).__name__}: {exc}")
