"""Best-effort failure alert - sent when the daily-job-search workflow job
fails, from any step, for any reason.

Deliberately defensive: this only runs (via `if: failure()` in the
workflow) after something has already gone wrong, so it must not itself
introduce a second, more confusing failure. Any problem building or sending
the alert is caught and printed, never raised - the workflow's own failed
conclusion is what actually notifies GitHub either way.

This can't cover every failure mode: if the run failed because the Gmail
token itself is broken, this alert (which also needs that same token) will
fail to send too. GitHub's own built-in email notification for failed
workflow runs (Settings -> Notifications -> Actions, on the account that
owns this repo) is the independent backup for exactly that case.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _log_tail(lines=60):
    """The run log's last lines, so the cause is readable from the email alone
    (the run page may be unreachable, e.g. on a network that blocks GitHub)."""
    for name in (os.environ.get("JOBAGENT_RUN_LOG", ""), "morning-run.log",
                 "logs/report-refresh.log"):
        path = ROOT / name if name else None
        if path and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(text[-lines:])
    return ""


def main():
    run_url = os.environ.get("GITHUB_RUN_URL", "")
    try:
        import yaml
        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        expected_account = (config.get("drive_output") or {}).get("expected_account", "")
        if not expected_account:
            print("drive_output.expected_account not configured; cannot send failure alert.")
            return 0
        subject = "Job Agent - today's run FAILED"
        body = (
            "The daily job search workflow failed today.\n\n"
            f"Run details: {run_url or '(run URL unavailable)'}\n\n"
            "Nothing was published or sent for today until this is fixed."
        )
        tail = _log_tail()
        if tail:
            body += "\n\nLast lines of the run log:\n\n" + tail
        from jobagent.notify import send_self_email
        result = send_self_email(subject, body, expected_account)
        print(f"Failure alert email sent: {result['message_id']}")
    except Exception as exc:
        print(f"Could not send failure alert email ({type(exc).__name__}: {exc}). "
              f"Run: {run_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
