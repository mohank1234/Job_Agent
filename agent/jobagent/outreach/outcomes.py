"""Observe Gmail evidence without equating drafts, sends, delivery and replies."""
from email.utils import getaddresses

from jobagent.runtime import now_iso
from .gmail import authenticate, get_authenticated_sender, GmailAuthError, SCOPES
from .service import EMAIL

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def summarize_threads(threads, sender, recipient):
    result = {"sent_message_ids": [], "incoming_message_ids": [],
              "automatic_response_ids": [], "possible_delivery_failure_ids": []}
    seen = set()
    for thread in threads:
        for message in thread.get("messages", []):
            mid = message["id"]
            if mid in seen:
                continue
            seen.add(mid)
            headers = {h["name"].lower(): h["value"] for h in message.get("payload", {}).get("headers", [])}
            from_addresses = {a.casefold() for _, a in getaddresses([headers.get("from", "")])}
            to_addresses = {a.casefold() for _, a in getaddresses([headers.get("to", "")])}
            if "SENT" in message.get("labelIds", []) and recipient.casefold() in to_addresses and sender.casefold() in from_addresses:
                result["sent_message_ids"].append(mid)
            elif sender.casefold() not in from_addresses and "DRAFT" not in message.get("labelIds", []):
                if any("mailer-daemon" in a or "postmaster" in a for a in from_addresses):
                    result["possible_delivery_failure_ids"].append(mid)
                elif headers.get("auto-submitted", "no").lower() != "no":
                    result["automatic_response_ids"].append(mid)
                else:
                    result["incoming_message_ids"].append(mid)
    result["status"] = "incoming_in_thread" if result["incoming_message_ids"] else "sent_no_incoming_in_thread" if result["sent_message_ids"] else "send_not_verified"
    return result


def observe(ledger, expected_sender):
    from googleapiclient.discovery import build
    try:
        creds = authenticate(required_scopes=SCOPES + [READ_SCOPE])
    except GmailAuthError as exc:
        if exc.kind == "consent_required":
            raise GmailAuthError("consent_required", "Run `python run.py gmail-auth --track-replies` interactively to grant read-only outcome access.") from exc
        raise
    sender = get_authenticated_sender(creds)
    if sender.casefold() != expected_sender.casefold():
        raise ValueError("Wrong Gmail account for outcome tracking")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    recipients = sorted({v["email"].strip().casefold() for v in ledger.values()})
    if any(not EMAIL.fullmatch(email) for email in recipients):
        raise ValueError("Invalid recipient in draft ledger")
    if len(recipients) > 100:
        raise ValueError("Outcome check limited to 100 recipients per invocation")
    observations = []
    for recipient in recipients:
        result = service.users().messages().list(userId="me", q=f"in:sent to:{recipient}", maxResults=100).execute()
        # A bounded query that overflowed must not masquerade as full history.
        threads, ids = [], set()
        for msg in result.get("messages", []):
            if msg["threadId"] not in ids:
                ids.add(msg["threadId"])
                threads.append(service.users().threads().get(userId="me", id=msg["threadId"],
                    format="metadata", metadataHeaders=["From", "To", "Auto-Submitted"]).execute())
        observations.append({"recipient": recipient, "truncated": bool(result.get("nextPageToken")),
                             **summarize_threads(threads, sender, recipient)})
    return {"checked_at": now_iso(), "sender": sender, "observations": observations,
            "limitation": "Sent label verifies sending, not delivery. Incoming thread messages need human review; replies in separate threads may be missed."}
