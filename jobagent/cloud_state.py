"""Private cloud checkpoints and one-time adoption of reviewed local state."""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from pathlib import Path, PurePosixPath

import yaml
from cryptography.fernet import Fernet

from jobagent.runtime import atomic_json

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ".jobagent-state.enc"
STATE_FILES = ("jobs.db", "drive_output_state.json", "outreach_draft_ledger.json",
               "gmail_report_drafts.json")


def state_path(root, name):
    path = PurePosixPath(name)
    if (path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
            or not (name in STATE_FILES or (len(path.parts) > 1 and path.parts[0] == "research"))):
        raise ValueError("Unexpected path in cloud checkpoint")
    target = (Path(root) / name).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise ValueError("Cloud checkpoint escapes the workspace")
    return target


def save_state(root, key):
    root = Path(root)
    buffer = io.BytesIO()
    paths = [root / name for name in STATE_FILES]
    paths.extend((root / "research").rglob("*"))
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            if path.is_file() and not path.is_symlink() and path.suffix not in (".lock", ".tmp"):
                name = path.relative_to(root).as_posix()
                state_path(root, name)
                archive.write(path, name)
    (root / ARCHIVE).write_bytes(Fernet(key).encrypt(buffer.getvalue()))


def restore_state(root, key):
    path = Path(root) / ARCHIVE
    if not path.exists():
        return False
    data = Fernet(key).decrypt(path.read_bytes())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [(state_path(root, item.filename), item) for item in archive.infolist()]
        for target, item in members:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(item))
    return True


def prepare_state(root, bootstrap, contacts):
    """Apply an explicit handoff once; retain newer runner state on later runs."""
    root = Path(root)
    marker = root / "research/morning-private/cloud-bootstrap.json"
    current = json.loads(marker.read_text()) if marker.exists() else {}
    if bootstrap and current.get("version") != bootstrap["version"]:
        atomic_json(root / "drive_output_state.json", bootstrap["drive_output_state"])
        ledger_path = root / "gmail_report_drafts.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        for key, entry in bootstrap["sent_outreach"].items():
            if entry.get("state") == "sent" and ledger.get(key, {}).get("sent_at", "") <= entry.get("sent_at", ""):
                ledger[key] = entry
        atomic_json(ledger_path, ledger)
        atomic_json(marker, {"version": bootstrap["version"]})
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    morning = config.get("morning", {})
    if contacts and morning.get("reviewed_contacts_file"):
        atomic_json(state_path(root, morning["reviewed_contacts_file"]), contacts)
    resume = morning.get("resume_attachment")
    if resume:
        path = state_path(root, resume)
        if not path.exists():
            if path.suffix.lower() != ".docx":
                raise ValueError("Configured resume is missing from the cloud checkpoint")
            sys.path.insert(0, str(root / "resume"))
            from build_resume import build_docx
            path.parent.mkdir(parents=True, exist_ok=True)
            build_docx(path)
        from jobagent.outreach.report_drafts import load_resume
        load_resume(path)
        from jobagent.morning import augment_manifest
        augment_manifest(path.parent, [path.name])
    return config


def verify_connections(config):
    from googleapiclient.discovery import build
    from jobagent.drive_output import authenticate_drive
    from jobagent.outreach.gmail import authenticate, get_authenticated_sender
    expected = config["drive_output"]["expected_account"]
    authenticate_drive(expected)
    if config.get("morning", {}).get("gmail_drafts"):
        credentials = authenticate(required_scopes=["https://www.googleapis.com/auth/gmail.compose"])
        if get_authenticated_sender(credentials).casefold() != expected.casefold():
            raise ValueError("Gmail account does not match the configured output account")
        build("gmail", "v1", credentials=credentials, cache_discovery=False).users().drafts().list(
            userId="me", maxResults=1).execute()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore", "prepare", "save"))
    args = parser.parse_args()
    if args.command == "restore":
        restored = restore_state(ROOT, os.environ["JOBAGENT_STATE_KEY"])
        print("Encrypted checkpoint restored." if restored else "No encrypted checkpoint yet.")
    elif args.command == "save":
        save_state(ROOT, os.environ["JOBAGENT_STATE_KEY"])
        print("Private checkpoint encrypted; credentials excluded.")
    else:
        config = prepare_state(ROOT, json.loads(os.environ.get("JOBAGENT_STATE_BOOTSTRAP_JSON") or "{}"),
                               json.loads(os.environ.get("JOBAGENT_REVIEWED_CONTACTS_JSON") or "{}"))
        verify_connections(config)
        print("Resume, reviewed contacts, sent-message records, Gmail and Drive validated.")


if __name__ == "__main__":
    main()
