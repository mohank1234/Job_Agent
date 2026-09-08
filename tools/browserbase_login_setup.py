"""Run this yourself: python tools/browserbase_login_setup.py

Creates a persistent Browserbase context + session for LinkedIn and for
Naukri, and prints a live-view URL for each. Open each URL in your own
browser, navigate to the site, and log in yourself (including any 2FA/
CAPTCHA) - your password is typed by you into that remote browser tab, it
never passes through this script or any AI.

Each session stays open for 15 minutes. Once you've logged in, the context
(cookies/session state) is saved under BROWSERBASE_API_KEY's project and
its ID is written to browserbase_contexts.json in the project root, so a
later automation step - and every future run of this script - can reuse
the authenticated context instead of logging in again or colliding on a
duplicate name.

Requires BROWSERBASE_API_KEY to be set (setx BROWSERBASE_API_KEY "...").
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from jobagent.enrich.browserbase import (BrowserbaseError, create_context,
                                          create_session, get_live_view_url)

ROOT = Path(__file__).parent.parent
CONTEXTS_FILE = ROOT / "browserbase_contexts.json"


def _with_retry(fn, *args, attempts: int = 3, **kwargs):
    """Transient network errors (timeout/connection_error) get a couple of
    retries with backoff - a single dropped SSL handshake shouldn't force a
    full re-run of the script. Real failures (unauthorized, forbidden,
    conflict) are NOT retried; they raise immediately."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except BrowserbaseError as exc:
            if exc.kind not in ("timeout", "connection_error", "server_error"):
                raise
            last_exc = exc
            if attempt < attempts - 1:
                print(f"  ({exc.kind}, retrying in {2 ** attempt}s...)")
                time.sleep(2 ** attempt)
    raise last_exc


def get_or_create_context(name: str, known_id: str | None) -> str:
    """Reuse a context ID already recorded from a previous run. Only create
    a new one if we don't have an ID on file - and if Browserbase says the
    name is already taken (or was created outside this script, e.g. during
    earlier testing), fall back to a numbered variant rather than failing
    outright."""
    if known_id:
        print(f"Reusing existing context '{name}': {known_id}")
        return known_id

    print(f"Creating context '{name}'...")
    candidate = name
    for suffix in range(1, 6):
        try:
            ctx_id = _with_retry(create_context, candidate)
            print(f"  context id: {ctx_id}")
            return ctx_id
        except BrowserbaseError as exc:
            if "already exists" in str(exc) or "Conflict" in str(exc):
                candidate = f"{name}-{suffix}"
                print(f"  name taken, trying '{candidate}'...")
                continue
            raise
    raise RuntimeError(f"Could not find a free context name starting from '{name}'")


def setup_one(name: str, known: dict) -> dict:
    ctx_id = get_or_create_context(name, known.get("context_id"))

    print("Starting a 15-minute session...")
    session = _with_retry(create_session, ctx_id, persist=True, timeout_seconds=900)
    print(f"  session id: {session['session_id']}")

    url = _with_retry(get_live_view_url, session["session_id"])
    print(f"\n  >>> Open this URL and log in to {name.split('-')[0].title()} yourself:")
    print(f"  {url}\n")

    return {"context_id": ctx_id, "session_id": session["session_id"]}


def main() -> int:
    existing = {}
    if CONTEXTS_FILE.exists():
        existing = json.loads(CONTEXTS_FILE.read_text(encoding="utf-8"))

    for name in ["linkedin-login", "naukri-login"]:
        try:
            existing[name] = setup_one(name, existing.get(name, {}))
        except BrowserbaseError as exc:
            print(f"FAILED for {name}: [{exc.kind}] {exc}")
            print("Continuing to the next one if any.\n")
        except RuntimeError as exc:
            print(f"FAILED for {name}: {exc}\n")

        CONTEXTS_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    print(f"Saved context IDs to {CONTEXTS_FILE}")
    print("\nYou have 15 minutes per session to complete login. If a session")
    print("times out before you finish, just re-run this script - it will")
    print("reuse the same context instead of creating a new one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
