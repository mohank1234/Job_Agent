"""One-time CI bootstrap: pull jobs.db from Drive so GitHub Actions continues
the same database the laptop already built, instead of starting a second one.

TEMPORARY. Delete this file and the workflow step that calls it once a
scheduled/manual run has published cleanly at least once - after that,
actions/cache carries jobs.db forward on its own and this is dead weight.
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
    with open("jobs.db", "wb") as f:
        f.write(buf.getvalue())
    print(f"jobs.db bootstrapped: {buf.tell()} bytes")


if __name__ == "__main__":
    main(sys.argv[1])
