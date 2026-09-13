"""Daily self-notification email - a status summary sent to the SAME
authenticated Gmail account, never a third party.

This is deliberately separate from jobagent/outreach/gmail.py, which stays
draft-only for outreach to real companies (that boundary exists because
sending unsolicited messages to strangers needs a policy this project
doesn't have yet - recipient confidence, suppression list, dedup ledger).
A status email to yourself carries none of that risk, so this module
actually sends.
"""
from __future__ import annotations

import base64
from email.mime.text import MIMEText

from jobagent.outreach.gmail import GmailAuthError, authenticate, get_authenticated_sender


def send_self_email(subject: str, body: str, expected_account: str, creds=None) -> dict:
    """Send a plain-text email from the authenticated account to itself.

    Raises GmailAuthError if the authenticated account is not exactly
    expected_account - this must never send to anyone else.
    """
    from googleapiclient.discovery import build

    creds = creds or authenticate()
    sender = get_authenticated_sender(creds)
    if sender.casefold() != expected_account.casefold():
        raise GmailAuthError("wrong_account", "Authenticated Gmail account differs from expected_account")
    if any(c in subject for c in "\r\n"):
        raise ValueError("Subject must be a single-line value")

    message = MIMEText(body)
    message["to"] = sender
    message["from"] = sender
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service = build("gmail", "v1", credentials=creds)
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"message_id": sent["id"], "sender": sender}
