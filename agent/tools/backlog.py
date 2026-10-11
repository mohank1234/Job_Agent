"""Read-only count using the same policy/freshness rules as run.py score."""
import sqlite3
import sys
from pathlib import Path
import yaml

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from jobagent.profile import load_profile
from jobagent.selection import pending_jobs

def main():
    db = root / "jobs.db"
    if not db.exists():
        raise FileNotFoundError("jobs.db is missing; backlog is unknown")
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    profile = load_profile(root / config.get("profile_file", "profile.yaml"))
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        print(len(pending_jobs(conn.execute("SELECT * FROM seen"), profile,
                               config.get("llm", {}), config.get("digest", {}).get("max_age_days"))))
    finally:
        conn.close()

if __name__ == "__main__":
    main()
