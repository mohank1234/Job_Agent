"""Gmail draft creation via OAuth — the authorized route to outreach email.

Deliberately DRAFT-ONLY. There is no send function here. Actual sending
needs the full policy machinery this project doesn't have yet: recipient
confidence checks, a suppression/do-not-contact list, a send ledger to
prevent duplicates across reruns, and an explicit user-enabled policy. Until
that exists, this module's job stops at "create a draft you can review in
Gmail and send yourself."

Scope: gmail.compose (draft creation now and, once a sending policy exists,
sending — without granting access to read arbitrary mail) plus
drive.readonly, added 2026-09-08 specifically to find and read the
"JobAgent Outreach Report" Sheet the cloud outreach routine writes to. Read-
only, and used for nothing else — never write, delete, or touch any other
file with this scope. drive.file (access only to files this app creates)
would be tighter but cannot read a pre-existing file it didn't create, and
there's no lighter scope that can find a file by name without either that
or a full Picker UI flow. Never request more than the feature in use needs.

Setup (see SETUP.md):
  1. Google Cloud Console -> enable Gmail API -> create an OAuth 2.0 Client
     ID (Desktop app) -> download as credentials.json in the project root.
  2. `python run.py gmail-auth` opens a browser for one-time consent and
     writes token.json (also gitignored) so future calls don't re-prompt.
  3. token.json is refreshed automatically when it expires; delete it to
     force re-authorization (e.g. after changing scopes or accounts) — this
     IS required after the drive.readonly scope was added, since an
     existing token only carries the scopes it was originally granted.

Official docs: https://developers.google.com/workspace/gmail/api/guides/sending
"""

from __future__ import annotations

import base64
import json
from email.mime.text import MIMEText
from pathlib import Path
from jobagent.runtime import atomic_json

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.readonly",
]

ROOT = Path(__file__).parent.parent.parent
CREDENTIALS_PATH = ROOT / "credentials.json"
TOKEN_PATH = ROOT / "token.json"


class GmailAuthError(Exception):
    """Raised when OAuth setup is missing or broken. `.kind` distinguishes
    'no_credentials' (credentials.json missing) from 'auth_failed' (the
    consent/refresh flow itself failed)."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


def authenticate(*, interactive=False, required_scopes=None):
    """Return valid Credentials, refreshing silently if possible. Only
    interactive=True permits a consent flow. Raises GmailAuthError if setup is
    missing — there is no way to proceed without it, and no default to fall
    back to."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GmailAuthError(
            "no_credentials",
            "Gmail packages not installed. Run: pip install "
            "google-api-python-client google-auth-httplib2 google-auth-oauthlib",
        ) from exc

    scopes = required_scopes or SCOPES
    creds = None
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        except (ValueError, KeyError) as exc:
            if not interactive:
                raise GmailAuthError("auth_failed", "Invalid token file; run gmail-auth to repair consent") from exc
        if creds and not creds.has_scopes(scopes):
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            atomic_json(TOKEN_PATH, json.loads(creds.to_json()))
            return creds
        except Exception as exc:
            if not interactive:
                raise GmailAuthError(
                    "auth_failed",
                    f"Token refresh failed ({type(exc).__name__}); run gmail-auth to re-authorize.",
                ) from exc
            creds = None

    if not interactive:
        raise GmailAuthError("consent_required", "Run `python run.py gmail-auth` interactively. Scheduled runs never open consent.")
    if not CREDENTIALS_PATH.exists():
        raise GmailAuthError(
            "no_credentials",
            f"{CREDENTIALS_PATH} not found. Download an OAuth 2.0 Desktop-app "
            f"client ID from Google Cloud Console and save it there — see "
            f"SETUP.md.",
        )

    # First-time consent: opens a browser, spins up a local server on
    # localhost to catch the redirect. One-time per machine/account.
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), scopes)
    creds = flow.run_local_server(port=0)
    if not creds.has_scopes(scopes):
        raise GmailAuthError("missing_scope", "Required Gmail/Drive scopes were not granted")
    atomic_json(TOKEN_PATH, json.loads(creds.to_json()))
    return creds


def get_authenticated_sender(creds=None) -> str:
    """Confirm which Gmail account is actually authenticated, so a draft is
    never created under the wrong account by accident."""
    from googleapiclient.discovery import build

    creds = creds or authenticate()
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def read_sheet_as_csv(file_name: str | None = None, creds=None, *, file_id=None) -> str:
    """Find a Google Sheet by exact name (Drive search) and return its
    content as CSV text (Drive's export endpoint, not the separate Sheets
    API — one scope covers both finding and reading). Raises GmailAuthError
    if no file with that exact name is found, or if more than one is (so a
    caller doesn't silently read the wrong one)."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds = creds or authenticate()
    drive = build("drive", "v3", credentials=creds)

    mime = "application/vnd.google-apps.spreadsheet"
    if file_id:
        meta = drive.files().get(fileId=file_id, fields="id,mimeType,trashed").execute()
        if meta.get("trashed") or meta.get("mimeType") != mime:
            raise GmailAuthError("invalid_source", "Configured file is not an active Google Sheet")
    else:
        if not file_name:
            raise GmailAuthError("invalid_source", "Configure outreach.gmail.sheet_id or pass --sheet-id")
        safe_name = file_name.replace("\\", "\\\\").replace("'", "\\'")
        files, token = [], None
        while True:
            resp = drive.files().list(
                q=f"name = '{safe_name}' and trashed = false and mimeType = '{mime}'",
                fields="nextPageToken,files(id,name)", pageSize=100, pageToken=token,
            ).execute()
            files.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        if len(files) != 1:
            raise GmailAuthError("ambiguous_source", f"Expected one Sheet named {file_name!r}, found {len(files)}; use its fixed ID")
        file_id = files[0]["id"]
    try:
        content = drive.files().export(fileId=file_id, mimeType="text/csv").execute()
    except HttpError as exc:
        raise GmailAuthError("invalid_response", f"Sheet export failed (HTTP {exc.resp.status})") from exc
    return content.decode("utf-8") if isinstance(content, bytes) else content


def create_draft(to: str, subject: str, body: str, creds=None, *, message_key=None, expected_sender=None) -> dict:
    """Create a Gmail draft. Returns {draft_id, message_id, sender}.

    Never sends. The draft sits in the authenticated account's Drafts folder
    for the human to review and send themselves, or for a future sending-
    policy feature to act on once one exists.
    """
    from googleapiclient.discovery import build

    creds = creds or authenticate()
    service = build("gmail", "v1", credentials=creds)
    sender = get_authenticated_sender(creds)
    if expected_sender and sender.casefold() != expected_sender.casefold():
        raise GmailAuthError("wrong_account", "Authenticated Gmail account differs from expected_sender")
    if any(c in to + subject for c in "\r\n"):
        raise ValueError("Recipient and subject must be single-line values")

    message = MIMEText(body)
    message["to"] = to
    message["from"] = sender
    message["subject"] = subject
    if message_key:
        message["Message-ID"] = f"<jobagent-{message_key}@draft.local>"
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    draft = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()

    return {
        "draft_id": draft["id"],
        "message_id": draft["message"]["id"],
        "sender": sender,
    }
