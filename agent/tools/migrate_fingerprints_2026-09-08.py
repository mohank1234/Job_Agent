"""One-time migration: recompute `seen.fingerprint` under the new formula.

Repo audit 2026-09-07, finding #2: the old fingerprint (company+title only)
collapsed distinct vacancies at the same company with the same title into one
row. The fix (jobagent/models.py Job.fingerprint) now includes the posting's
URL. Every row already stored under the old formula must be recomputed so
`record_match`/`record_classification` (keyed on the freshly-computed
fingerprint of a `row_to_job`-reconstructed Job) can still find its row.

Safe by construction: `jobs.db` was file-copied to a timestamped .bak before
this ran. This script does not delete rows or columns, only rewrites the
`fingerprint` primary-key column's values, and aborts before writing anything
if the new formula would produce a duplicate key.

Usage:  python tools/migrate_fingerprints_2026-09-08.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from jobagent.models import normalize  # noqa: E402

DB_PATH = Path(__file__).parent.parent / "jobs.db"


def new_fingerprint(company: str, title: str, url: str, location: str) -> str:
    import hashlib
    disambiguator = normalize(url or "") or normalize(location or "")
    key = f"{normalize(company or '')}|{normalize(title or '')}|{disambiguator}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def main() -> int:
    if not DB_PATH.exists():
        print(f"No jobs.db at {DB_PATH} — nothing to migrate.")
        return 0

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT fingerprint, company, title, url, location FROM seen"
    ).fetchall()
    print(f"Read {len(rows)} rows from seen.")

    updates: list[tuple[str, str]] = []  # (new_fp, old_fp)
    seen_new: dict[str, str] = {}
    collisions = 0
    for r in rows:
        new_fp = new_fingerprint(r["company"], r["title"], r["url"], r["location"])
        if new_fp in seen_new and seen_new[new_fp] != r["fingerprint"]:
            collisions += 1
            print(
                f"  COLLISION: old fps {seen_new[new_fp]!r} and "
                f"{r['fingerprint']!r} both map to new fp {new_fp!r} "
                f"({r['company']!r} / {r['title']!r})"
            )
            continue
        seen_new[new_fp] = r["fingerprint"]
        if new_fp != r["fingerprint"]:
            updates.append((new_fp, r["fingerprint"]))

    print(f"{len(updates)} rows need a new fingerprint; {collisions} collisions detected.")
    if collisions:
        print("Aborting — resolve collisions before migrating. No rows changed.")
        conn.close()
        return 1

    if not updates:
        print("Nothing to do — all fingerprints already match the new formula.")
        conn.close()
        return 0

    conn.execute("BEGIN")
    try:
        for new_fp, old_fp in updates:
            conn.execute(
                "UPDATE seen SET fingerprint = ? WHERE fingerprint = ?",
                (new_fp, old_fp),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    after = conn.execute("SELECT COUNT(*) c FROM seen").fetchone()["c"]
    distinct_fps = conn.execute(
        "SELECT COUNT(DISTINCT fingerprint) c FROM seen"
    ).fetchone()["c"]
    print(f"Migration committed. Row count after: {after} (was {len(rows)}).")
    print(f"Distinct fingerprints after: {distinct_fps} (should equal row count).")
    conn.close()
    return 0 if after == len(rows) and distinct_fps == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
