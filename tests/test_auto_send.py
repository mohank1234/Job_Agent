import base64
import csv
import json
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace

from jobagent.outreach.report_drafts import sync_report_drafts
from jobagent.outreach.followup import send_followups


class FakeAutoSendGmail:
    """Minimal fake covering drafts create/get/send and messages/threads
    just far enough to exercise the auto-send and follow-up paths."""

    def __init__(self):
        self.drafts_data = {}
        self.messages_data = {}
        self.threads_data = {}
        self._draft_seq = 0
        self._message_seq = 0

    def users(self):
        return self

    def drafts(self):
        return _Drafts(self)

    def messages(self):
        return _Messages(self)

    def threads(self):
        return _Threads(self)

    def response(self, value):
        return SimpleNamespace(execute=lambda: value)

    def getProfile(self, **kwargs):
        return self.response({"emailAddress": "candidate@example.com"})


class _Drafts:
    def __init__(self, root):
        self.root = root

    def list(self, **kwargs):
        return self.root.response({"drafts": [{"id": k} for k in self.root.drafts_data]})

    def get(self, id, **kwargs):
        return self.root.response(self.root.drafts_data[id])

    def create(self, body, **kwargs):
        self.root._draft_seq += 1
        key = str(self.root._draft_seq)
        self.root.drafts_data[key] = {"id": key, "message": {**body["message"], "id": "message-" + key}}
        return self.root.response(self.root.drafts_data[key])

    def update(self, id, body, **kwargs):
        self.root.drafts_data[id]["message"].update(body["message"])
        return self.root.response(self.root.drafts_data[id])

    def send(self, body, **kwargs):
        draft = self.root.drafts_data[body["id"]]
        self.root._message_seq += 1
        message_id = "sent-" + str(self.root._message_seq)
        thread_id = "thread-" + str(self.root._message_seq)
        self.root.messages_data[message_id] = {
            "id": message_id, "threadId": thread_id,
            "payload": {"headers": [
                {"name": "Message-ID", "value": f"<{message_id}@example.com>"},
                {"name": "From", "value": "candidate@example.com"},
                {"name": "To", "value": "alex@example.com"},
            ]},
            "raw": draft["message"]["raw"], "labelIds": ["SENT"],
        }
        self.root.threads_data[thread_id] = {"messages": [self.root.messages_data[message_id]]}
        return self.root.response({"id": message_id, "threadId": thread_id})


class _Messages:
    def __init__(self, root):
        self.root = root

    def get(self, id, **kwargs):
        return self.root.response(self.root.messages_data[id])

    def send(self, body, **kwargs):
        self.root._message_seq += 1
        message_id = "followup-" + str(self.root._message_seq)
        thread_id = body.get("threadId", "thread-unknown")
        record = {"id": message_id, "threadId": thread_id,
                  "payload": {"headers": [{"name": "From", "value": "candidate@example.com"},
                                          {"name": "To", "value": "alex@example.com"}]},
                  "labelIds": ["SENT"]}
        self.root.messages_data[message_id] = record
        self.root.threads_data.setdefault(thread_id, {"messages": []})["messages"].append(record)
        return self.root.response({"id": message_id, "threadId": thread_id})


class _Threads:
    def __init__(self, root):
        self.root = root

    def get(self, id, **kwargs):
        return self.root.response(self.root.threads_data[id])


def _write_row(tmp_path, **overrides):
    row = {
        "Company": "Example", "Job Link": "https://jobs.example/1", "Job Title": "SDET",
        "Cold Email Subject": "SDET at Example",
        "Cold Email": "Hi Alex,\n\nI built API tests. Could we talk?\n\nThanks,\nCandidate",
        "Public Work Email": "alex@example.com", "Email Source": "https://example.com/team",
        "Email Evidence": "Public hiring contact", "Draft Status": "Draft only",
    }
    row.update(overrides)
    with (tmp_path / "Startup Outreach.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    return row


def test_auto_send_sends_freshly_created_draft_and_records_ledger(tmp_path):
    _write_row(tmp_path)
    service = FakeAutoSendGmail()
    ledger_path = tmp_path / "ledger.json"
    result = sync_report_drafts(tmp_path, "candidate@example.com", ledger_path=ledger_path,
                                 service=service, auto_send=True)[0]
    assert result["Status"] == "Sent automatically (auto-send enabled)"
    assert result["Approval"] == "Sent automatically; no manual review step"
    ledger = json.loads(ledger_path.read_text())
    entry = next(iter(ledger.values()))
    assert entry["state"] == "sent"
    assert entry["sent_to"] == "alex@example.com"
    assert entry["thread_id"] and entry["sent_message_id"]
    assert entry["rfc_message_id"] == f"<{entry['sent_message_id']}@example.com>"
    assert entry["signature"] == "Thanks,\nCandidate"

    # A second run must never re-send - dedup via the existing ledger state.
    result2 = sync_report_drafts(tmp_path, "candidate@example.com", ledger_path=ledger_path,
                                  service=service, auto_send=True)[0]
    assert result2["Status"] == "Already sent; no duplicate draft created"


def test_auto_send_never_sends_preserved_user_edits(tmp_path):
    row = _write_row(tmp_path)
    service = FakeAutoSendGmail()
    ledger_path = tmp_path / "ledger.json"
    # First pass without auto_send creates a plain draft (as if the feature
    # was off, or a stray pre-existing draft is sitting there).
    sync_report_drafts(tmp_path, "candidate@example.com", ledger_path=ledger_path, service=service)
    draft_id = next(iter(service.drafts_data))
    from jobagent.outreach.report_drafts import decode_draft
    decoded = decode_draft(service.drafts_data[draft_id])
    message = EmailMessage()
    message["Subject"] = decoded["subject"]
    message["X-JobAgent-Draft-Key"] = decoded["key"]
    message.set_content("My own personal edits, not ready to send")
    service.drafts_data[draft_id]["message"]["raw"] = base64.urlsafe_b64encode(message.as_bytes()).decode()

    result = sync_report_drafts(tmp_path, "candidate@example.com", ledger_path=ledger_path,
                                 service=service, auto_send=True)[0]
    assert "preserved" in result["Status"]
    assert result["Status"] != "Sent automatically (auto-send enabled)"
    ledger = json.loads(ledger_path.read_text())
    assert next(iter(ledger.values()))["state"] != "sent"


def test_followup_sent_after_delay_when_no_reply(tmp_path):
    _write_row(tmp_path)
    service = FakeAutoSendGmail()
    ledger_path = tmp_path / "ledger.json"
    sync_report_drafts(tmp_path, "candidate@example.com", ledger_path=ledger_path,
                        service=service, auto_send=True)
    ledger = json.loads(ledger_path.read_text())
    key, entry = next(iter(ledger.items()))
    old_sent_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    entry["sent_at"] = old_sent_at
    ledger_path.write_text(json.dumps(ledger))

    results = send_followups("candidate@example.com", ledger_path=ledger_path, service=service, after_days=5)
    assert results[0]["status"] == "Follow-up sent"
    ledger = json.loads(ledger_path.read_text())
    assert ledger[key]["followup_sent_at"]
    assert ledger[key]["followup_message_id"]

    # Never a second follow-up.
    results_again = send_followups("candidate@example.com", ledger_path=ledger_path, service=service, after_days=5)
    assert results_again == []


def test_followup_skipped_before_delay_and_when_replied(tmp_path):
    _write_row(tmp_path)
    service = FakeAutoSendGmail()
    ledger_path = tmp_path / "ledger.json"
    sync_report_drafts(tmp_path, "candidate@example.com", ledger_path=ledger_path,
                        service=service, auto_send=True)
    ledger = json.loads(ledger_path.read_text())
    key, entry = next(iter(ledger.items()))

    # Too recent - no follow-up yet.
    assert send_followups("candidate@example.com", ledger_path=ledger_path, service=service, after_days=5) == []

    # Old enough, but a reply exists in the thread - must not follow up.
    entry["sent_at"] = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    ledger_path.write_text(json.dumps(ledger))
    thread_id = entry["thread_id"]
    service.threads_data[thread_id]["messages"].append({
        "id": "reply-1", "labelIds": ["INBOX"],
        "payload": {"headers": [{"name": "From", "value": "alex@example.com"},
                                {"name": "To", "value": "candidate@example.com"}]},
    })
    results = send_followups("candidate@example.com", ledger_path=ledger_path, service=service, after_days=5)
    assert results[0]["status"] == "Skipped: reply already received"
    ledger = json.loads(ledger_path.read_text())
    assert "followup_sent_at" not in ledger[key]
