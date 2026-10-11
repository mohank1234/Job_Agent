import json
from argparse import Namespace
from datetime import datetime, timezone

import run
from jobagent.models import Job


def test_mailbox_export_writes_only_linkedin_jobs_for_job_link(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "load_yaml", lambda name: {"sources": {"mailbox": {"enabled": True, "user": "a@example.com"}}})
    seen = {}

    def fake_fetch(mail):
        seen["senders"] = mail["senders"]
        stamp = datetime(2026, 10, 11, 4, 0, tzinfo=timezone.utc)
        return [
            Job(source="mail/linkedin", company="Acme", title="QA Engineer", url="https://www.linkedin.com/jobs/view/123/", posted_at=stamp),
            Job(source="mail/naukri", company="Other", title="SDET", url="https://www.naukri.com/job-listings-1"),
        ], []

    monkeypatch.setattr(run, "fetch_mailbox_jobs", fake_fetch)
    out = tmp_path / "alerts.json"
    assert run.cmd_mailbox_export(Namespace(out=str(out))) == 0
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert seen["senders"] == ["linkedin"]
    assert [r["url"] for r in rows] == ["https://www.linkedin.com/jobs/view/123/"]
    assert rows[0]["company"] == "Acme" and rows[0]["receivedAt"].startswith("2026-10-11T04:00")


def test_mailbox_export_does_nothing_when_the_mailbox_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "load_yaml", lambda name: {"sources": {"mailbox": {"enabled": False}}})
    assert run.cmd_mailbox_export(Namespace(out=str(tmp_path / "x.json"))) == 1
