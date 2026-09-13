"""One-time CI bootstrap: pull the encrypted state checkpoint from Drive so
GitHub Actions adopts the laptop's just-corrected, in-sync checkpoint instead
of the stale one already in actions/cache.

TEMPORARY. Delete this file and the workflow step that calls it once a
scheduled/manual run has published cleanly at least once - after that,
actions/cache carries .jobagent-state.enc forward on its own.
"""
import io
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


def main(file_id: str) -> None:
    creds = Credentials.from_authorized_user_file("drive_token.json")
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    drive = build("drive", "v3", credentials=creds)
    request = drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    with open(".jobagent-state.enc", "wb") as f:
        f.write(buf.getvalue())
    print(f".jobagent-state.enc bootstrapped: {buf.tell()} bytes")


if __name__ == "__main__":
    main(sys.argv[1])
