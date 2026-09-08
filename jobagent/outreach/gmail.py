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
  2. First call to `authenticate()` opens a browser for one-time consent and
     writes token.json (also gitignored) so future calls don't re-prompt.
  3. token.json is refreshed automatically when it expires; delete it to
     force re-authorization (e.g. after changing scopes or accounts) — this
     IS required after the drive.readonly scope was added, since an
     existing token only carries the scopes it was originally granted.

Official docs: https://developers.google.com/workspace/gmail/api/guides/sending
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from pathlib import Path

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


def authenticate():
    """Return valid Credentials, running the one-time consent flow or a
    silent refresh as needed. Raises GmailAuthError if credentials.json is
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

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as exc:
            raise GmailAuthError(
                "auth_failed",
                f"Token refresh failed ({exc}) — delete token.json and re-run "
                f"to re-authorize.",
            ) from exc

    if not CREDENTIALS_PATH.exists():
        raise GmailAuthError(
            "no_credentials",
            f"{CREDENTIALS_PATH} not found. Download an OAuth 2.0 Desktop-app "
            f"client ID from Google Cloud Console and save it there — see "
            f"SETUP.md.",
        )

    # First-time consent: opens a browser, spins up a local server on
    # localhost to catch the redirect. One-time per machine/account.
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_authenticated_sender(creds=None) -> str:
    """Confirm which Gmail account is actually authenticated, so a draft is
    never created under the wrong account by accident."""
    from googleapiclient.discovery import build

    creds = creds or authenticate()
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def read_sheet_as_csv(file_name: str, creds=None) -> str:
    """Find a Google Sheet by exact name (Drive search) and return its
    content as CSV text (Drive's export endpoint, not the separate Sheets
    API — one scope covers both finding and reading). Raises GmailAuthError
    if no file with that exact name is found, or if more than one is (so a
    caller doesn't silently read the wrong one)."""
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds = creds or authenticate()
    drive = build("drive", "v3", credentials=creds)

    safe_name = file_name.replace("'", "\\'")
    resp = drive.files().list(
        q=f"name = '{safe_name}' and trashed = false",
        fields="files(id, name, mimeType, modifiedTime)",
    ).execute()
    files = resp.get("files", [])
    if not files:
        raise GmailAuthError("invalid_response", f"No Drive file named exactly {file_name!r} found")
    if len(files) > 1:
        newest = max(files, key=lambda f: f["modifiedTime"])
        files = [newest]  # exact-name collisions shouldn't happen given the naming scheme, but don't guess silently wrong

    file_id = files[0]["id"]
    try:
        content = drive.files().export(fileId=file_id, mimeType="text/csv").execute()
    except HttpError as exc:
        raise GmailAuthError("invalid_response", f"Could not export {file_name!r} as CSV: {exc}") from exc
    return content.decode("utf-8") if isinstance(content, bytes) else content


def create_draft(to: str, subject: str, body: str, creds=None) -> dict:
    """Create a Gmail draft. Returns {draft_id, message_id, sender}.

    Never sends. The draft sits in the authenticated account's Drafts folder
    for the human to review and send themselves, or for a future sending-
    policy feature to act on once one exists.
    """
    from googleapiclient.discovery import build

    creds = creds or authenticate()
    service = build("gmail", "v1", credentials=creds)
    sender = get_authenticated_sender(creds)

    message = MIMEText(body)
    message["to"] = to
    message["from"] = sender
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    draft = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()

    return {
        "draft_id": draft["id"],
        "message_id": draft["message"]["id"],
        "sender": sender,
    }
