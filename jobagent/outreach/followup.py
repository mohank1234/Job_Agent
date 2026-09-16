"""Send at most one automatic follow-up for outreach messages that got no reply.

Only fires for gmail_report_drafts.json ledger entries already in state
'sent' by report_drafts.sync_report_drafts's auto_send path - never a blind
resend, never more than one follow-up per company, and never if a reply
(anything incoming in that message's own thread) has been observed.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from jobagent.outreach.gmail import GmailAuthError, SCOPES, authenticate, get_authenticated_sender
from jobagent.outreach.outcomes import summarize_threads
from jobagent.runtime import atomic_json, now_iso, process_lock

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "gmail_report_drafts.json"

FOLLOWUP_TEMPLATE = (
    "Hi again,\n\n"
    "Just following up on my note below in case it got buried - still very "
    "interested in the {role} role at {company}. Happy to share more "
    "information or jump on a quick call whenever convenient.\n\n"
    "{signature}"
)


def _parse_iso(value):
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def send_followups(expected_sender, *, ledger_path=LEDGER, service=None, after_days=5, now=None):
    if not expected_sender:
        raise ValueError("Expected Gmail account is required")
    if service is None:
        from googleapiclient.discovery import build
        creds = authenticate(required_scopes=SCOPES)
        sender = get_authenticated_sender(creds)
        if sender.casefold() != expected_sender.casefold():
            raise GmailAuthError("wrong_account", "Gmail account differs from configured output account")
        service = build("gmail", "v1", credentials=creds)
    else:
        sender = service.users().getProfile(userId="me").execute()["emailAddress"]
        if sender.casefold() != expected_sender.casefold():
            raise ValueError("Wrong Gmail account")
    now = now or datetime.now(timezone.utc)
    ledger_path = Path(ledger_path)
    results = []
    with process_lock(ledger_path.with_suffix(".lock")):
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
        for key, entry in ledger.items():
            if entry.get("state") != "sent" or entry.get("followup_sent_at"):
                continue
            sent_at = _parse_iso(entry.get("sent_at", ""))
            thread_id, to = entry.get("thread_id"), entry.get("sent_to")
            if not sent_at or not thread_id or not to:
                continue
            if (now - sent_at).days < after_days:
                continue
            company = entry.get("company", "your company")
            thread = service.users().threads().get(
                userId="me", id=thread_id, format="metadata",
                metadataHeaders=["From", "To", "Auto-Submitted"],
            ).execute()
            observed = summarize_threads([thread], sender, to)
            if observed["incoming_message_ids"]:
                entry["followup_skipped_reason"] = "replied"
                ledger[key] = entry
                atomic_json(ledger_path, ledger)
                results.append({"company": company, "status": "Skipped: reply already received"})
                continue
            message = EmailMessage()
            message["From"], message["To"] = sender, to
            message["Subject"] = f"Re: {entry['subject']}" if entry.get("subject") else "Following up"
            if entry.get("rfc_message_id"):
                message["In-Reply-To"] = entry["rfc_message_id"]
                message["References"] = entry["rfc_message_id"]
            message.set_content(FOLLOWUP_TEMPLATE.format(
                role=entry.get("role") or "role", company=company, signature=entry.get("signature", ""),
            ))
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            try:
                sent = service.users().messages().send(
                    userId="me", body={"raw": raw, "threadId": thread_id}
                ).execute()
                entry.update(followup_sent_at=now_iso(), followup_message_id=sent["id"])
                results.append({"company": company, "status": "Follow-up sent"})
            except Exception as exc:
                entry["followup_error"] = type(exc).__name__
                results.append({"company": company, "status": f"Follow-up failed: {type(exc).__name__}"})
            ledger[key] = entry
            atomic_json(ledger_path, ledger)
    return results
