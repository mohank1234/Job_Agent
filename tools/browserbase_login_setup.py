"""Human sign-in using hosted browsers. No passwords, OTPs or applications are entered by this script.

Run: python tools/browserbase_login_setup.py --site both
Requires optional requirements-browser.txt and BROWSERBASE_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jobagent.enrich.browserbase import (create_context, create_session, get_context,
                                         get_session, get_live_view_url, end_session)
from jobagent.runtime import atomic_json, now_iso, process_lock

CONTEXTS_FILE = ROOT / "browserbase_contexts.json"
LOGIN_URLS = {"linkedin": "https://www.linkedin.com/login",
              "naukri": "https://www.naukri.com/nlogin/login"}


def signed_in(page, site):
    """Require account navigation evidence; reaching a login page is not login."""
    if "login" in page.url.lower() or "checkpoint" in page.url.lower():
        return False
    selector = 'nav a[href*="/in/"]' if site == "linkedin" else 'a[href*="/mnjuser/profile"]'
    return page.locator(selector).count() > 0 and page.locator('input[type="password"]:visible').count() == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", choices=["linkedin", "naukri", "both"], default="both")
    args = parser.parse_args()
    # Check the optional controller BEFORE creating billable sessions.
    from playwright.sync_api import sync_playwright
    sites = list(LOGIN_URLS) if args.site == "both" else [args.site]
    with process_lock(CONTEXTS_FILE.with_suffix(".lock")):
        state = json.loads(CONTEXTS_FILE.read_text(encoding="utf-8")) if CONTEXTS_FILE.exists() else {}
        sessions, controllers = [], []
        verified = set()
        try:
            with sync_playwright() as playwright:
                for site in sites:
                    name = site + "-login"
                    item = state.setdefault(name, {})
                    if item.get("state") in ("creating_context", "creating_session"):
                        raise RuntimeError("A previous creation outcome is uncertain; inspect Browserbase before starting another session")
                    context_id = item.get("context_id")
                    if context_id:
                        get_context(context_id)
                    else:
                        item["state"] = "creating_context"
                        atomic_json(CONTEXTS_FILE, state)
                        context_id = create_context(name)  # never retry non-idempotent creation
                        item.update(context_id=context_id, state="context_ready")
                        atomic_json(CONTEXTS_FILE, state)
                    metadata = get_session(item["session_id"]) if item.get("session_id") else {}
                    if metadata.get("status") == "RUNNING":
                        session_id = item["session_id"]
                    else:
                        item["state"] = "creating_session"
                        atomic_json(CONTEXTS_FILE, state)
                        session = create_session(context_id, persist=True, timeout_seconds=900)
                        session_id = session["session_id"]
                        item.update(session_id=session_id, state="login_pending", started_at=now_iso())
                        atomic_json(CONTEXTS_FILE, state)
                        metadata = get_session(session_id)
                    sessions.append(session_id)
                    browser = playwright.chromium.connect_over_cdp(metadata["connectUrl"])
                    context = browser.contexts[0]
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(LOGIN_URLS[site], wait_until="domcontentloaded", timeout=60000)
                    controllers.append((site, browser, page))
                    print(f"{site}: open this temporary live view and sign in yourself (including OTP/CAPTCHA):", flush=True)
                    print(get_live_view_url(session_id), flush=True)
                deadline = time.monotonic() + 780
                while time.monotonic() < deadline and len(verified) < len(sites):
                    for site, browser, page in controllers:
                        if not browser.is_connected():
                            continue
                        if site not in verified and signed_in(page, site):
                            verified.add(site)
                            state[site + "-login"].update(state="login_verified", verified_at=now_iso())
                            atomic_json(CONTEXTS_FILE, state)
                            print(f"{site}: account navigation confirmed; no application submitted.", flush=True)
                    if len(verified) < len(sites):
                        time.sleep(10)
                return 0 if len(verified) == len(sites) else 2
        finally:
            for session_id in sessions:
                try:
                    end_session(session_id)
                except Exception:
                    print("Session release could not be confirmed; its configured timeout still applies.", flush=True)
            print("Login sessions ended. Unconfirmed logins must be checked before account actions.", flush=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Browser connection exceptions may contain a credential-bearing CDP URL.
        print(f"Login setup failed: {type(exc).__name__}. Check Browserbase session state; no automatic creation retry.", flush=True)
        raise SystemExit(1)
