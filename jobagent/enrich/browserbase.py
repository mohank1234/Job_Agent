"""Browserbase adapter — hosted browser sessions with persistent login state.

Used ONLY for a human-in-the-loop login flow: this module creates a session
and hands back a live-view URL for the HUMAN to open in their own browser,
log in, and complete any 2FA/CAPTCHA themselves. The actual username/
password is never typed by this code or seen by it — only the resulting
authenticated browser context (cookies/session state) is reused afterward.

This exists specifically to support logging into LinkedIn/Naukri for
read/write automation on those sites — a deliberate policy exception
requested and approved by the user on 2026-09-08, made with the account-ban
risk (both platforms prohibit automated access in their ToS) explained and
accepted. That risk is real and belongs to the account owner, not this
code; nothing here tries to evade detection.

Public documented API: https://docs.browserbase.com/reference/api
Requires BROWSERBASE_API_KEY (free tier: 1 browser hour, 3 concurrent
browsers, checked 2026-09-08 via browserbase.com/pricing).
"""

from __future__ import annotations

import os

import httpx

from ._errors import AdapterError, classify_http_error

API_BASE = "https://api.browserbase.com/v1"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

BrowserbaseError = AdapterError


def _headers() -> dict:
    api_key = os.environ.get("BROWSERBASE_API_KEY", "")
    if not api_key:
        raise BrowserbaseError("unauthorized", "BROWSERBASE_API_KEY is not set")
    return {"X-BB-API-Key": api_key, "Content-Type": "application/json"}


def create_context(name: str) -> str:
    """Create a persistent, named browser context (a saved cookie jar /
    login state). Returns the context id. Names are unique per project —
    creating one with an existing name will fail; use get-or-create logic
    at the call site if re-running this is expected."""
    try:
        resp = httpx.post(
            f"{API_BASE}/contexts",
            headers=_headers(),
            json={"name": name},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["id"]
    except Exception as exc:
        if isinstance(exc, BrowserbaseError):
            raise
        raise classify_http_error(exc, "Browserbase") from exc


def create_session(context_id: str, persist: bool = True, timeout_seconds: int = 900) -> dict:
    """Start a browser session bound to a context. Returns
    {session_id, connect_url}. Set persist=True so whatever the human logs
    into during this session (cookies, local storage) is saved back to the
    context for reuse in future sessions — without this, every session
    starts logged out again. Default timeout is 15 minutes, long enough for
    a human to complete a login plus 2FA without being cut off mid-flow."""
    try:
        resp = httpx.post(
            f"{API_BASE}/sessions",
            headers=_headers(),
            json={
                "browserSettings": {
                    "context": {"id": context_id, "persist": persist},
                    "recordSession": False,
                    "logSession": False,
                    "solveCaptchas": False,
                },
                "timeout": timeout_seconds,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"session_id": data["id"], "connect_url": data.get("connectUrl")}
    except Exception as exc:
        if isinstance(exc, BrowserbaseError):
            raise
        raise classify_http_error(exc, "Browserbase") from exc


def get_live_view_url(session_id: str) -> str:
    """The URL a HUMAN opens to watch/click/type into the session in real
    time — this is how the actual login (including 2FA/CAPTCHA) gets done,
    without this code ever handling a password."""
    try:
        resp = httpx.get(
            f"{API_BASE}/sessions/{session_id}/debug",
            headers=_headers(),
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["debuggerFullscreenUrl"]
    except Exception as exc:
        if isinstance(exc, BrowserbaseError):
            raise
        raise classify_http_error(exc, "Browserbase") from exc


def get_session(session_id: str) -> dict:
    """Read session metadata. connectUrl contains credentials: never log it."""
    try:
        resp = httpx.get(f"{API_BASE}/sessions/{session_id}", headers=_headers(), timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        if isinstance(exc, BrowserbaseError):
            raise
        raise classify_http_error(exc, "Browserbase") from exc


def get_context(context_id: str) -> dict:
    try:
        resp = httpx.get(f"{API_BASE}/contexts/{context_id}", headers=_headers(), timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        if isinstance(exc, BrowserbaseError):
            raise
        raise classify_http_error(exc, "Browserbase") from exc


def end_session(session_id: str) -> None:
    """Explicitly stop a session (releases the concurrent-session slot)."""
    try:
        resp = httpx.post(
            f"{API_BASE}/sessions/{session_id}",
            headers=_headers(),
            json={"status": "REQUEST_RELEASE"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as exc:
        if isinstance(exc, BrowserbaseError):
            raise
        raise classify_http_error(exc, "Browserbase") from exc
