"""Validated input and crash-safe drafts. Uncertain outcomes require review."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from jobagent.runtime import atomic_json, now_iso, process_lock

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
REQUIRED = {"Company", "Email", "Email Subject", "Email Body"}


def parse_rows(text):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = reader.fieldnames or []
    missing = REQUIRED - set(headers)
    if missing or len(headers) != len(set(headers)):
        raise ValueError(f"Invalid outreach schema; missing columns: {sorted(missing)}; headers must be unique")
    rows = list(reader)
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError("Malformed CSV row: column count differs from header")
    return rows


def recipient_key(company, email):
    # Same inbox, renamed company/contact/row: still one approach.
    return hashlib.sha256(email.strip().casefold().encode()).hexdigest()[:24]


def recent(value, max_age_days=7):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        return 0 <= age <= max_age_days
    except (ValueError, TypeError, AttributeError):
        return False


def validate_row(row):
    if not EMAIL.fullmatch(row.get("Email", "").strip()):
        return "missing_or_invalid_email"
    if not all(row.get(k, "").strip() for k in REQUIRED):
        return "missing_content"
    if any(c in row["Email Subject"] for c in "\r\n"):
        return "invalid_subject"
    if row.get("Contact Verification") != "public_email_found" or not recent(row.get("Contact Verified At")):
        return "contact_unverified_or_stale"
    if not row.get("Contact Source URL", "").startswith("https://"):
        return "contact_source_missing"
    if row.get("JD Status") != "verified_live" or not recent(row.get("JD Verified At")):
        return "opening_unverified_or_stale"
    if row.get("Content Status") != "reviewed" or row.get("Fit Status") != "in_scope":
        return "content_or_fit_needs_review"
    if len(row.get("JD Text", "").strip()) < 200:
        return "full_jd_missing"
    if re.search(r"\[(?:your|insert|name|company|role)\b", row["Email Body"], re.I):
        return "placeholder_content"
    return None


def draft_rows(rows, ledger_path: Path, create, *, dry_run=False, limit=10):
    """Reserve before creating. Never automatically retry pending/uncertain.

    Gmail drafts.create has no idempotency key: after a crash an absent draft
    may have been sent or deleted. Inspect the recipient before clearing state.
    """
    if limit < 0:
        raise ValueError("Draft limit must be non-negative")
    def work():
        ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
        if not isinstance(ledger, dict) or any(not isinstance(v, dict) or not EMAIL.fullmatch(v.get("email", "").strip()) for v in ledger.values()):
            raise ValueError("Invalid ledger; refusing to risk duplicate drafts")
        used = {v["email"].strip().casefold() for v in ledger.values()}
        result = {"created": 0, "planned": 0, "skipped": {}, "uncertain": 0}
        for row in rows:
            email = row.get("Email", "").strip().casefold()
            reason = "already_reserved_or_drafted" if email in used else validate_row(row)
            if reason:
                result["skipped"][reason] = result["skipped"].get(reason, 0) + 1
                continue
            if result["planned"] >= limit:
                result["skipped"]["over_limit"] = result["skipped"].get("over_limit", 0) + 1
                continue
            used.add(email)
            result["planned"] += 1
            if dry_run:
                continue
            key = recipient_key(row["Company"], email)
            entry = {"company": row["Company"], "email": email, "state": "pending",
                     "reserved_at": now_iso(), "message_key": key,
                     "job_url": row.get("Job Link"), "contact_source": row.get("Contact Source URL"),
                     "content_hash": hashlib.sha256((row["Email Subject"] + "\n" + row["Email Body"]).encode()).hexdigest()}
            ledger[key] = entry
            atomic_json(ledger_path, ledger)
            try:
                draft = create(to=email, subject=row["Email Subject"], body=row["Email Body"], message_key=key)
            except Exception as exc:
                entry.update(state="uncertain", error_type=type(exc).__name__)
                atomic_json(ledger_path, ledger)
                result["uncertain"] += 1
                break
            entry.update(draft, state="drafted", drafted_at=now_iso())
            atomic_json(ledger_path, ledger)
            result["created"] += 1
        return result
    if dry_run:
        return work()
    with process_lock(ledger_path.with_suffix(".lock")):
        return work()
