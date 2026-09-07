"""Print how many candidates still need an LLM verdict.

A one-line helper so shell scripts do not have to embed a quoted SQL string,
which is where Run-Daily.ps1 previously broke.
"""

import sqlite3
import sys
from pathlib import Path

db = Path(__file__).resolve().parent.parent / "jobs.db"
if not db.exists():
    print(0)
    sys.exit(0)

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
n = conn.execute(
    "SELECT COUNT(*) FROM seen WHERE candidate = 1 "
    "AND (scored_by IS NULL OR scored_by != 'llm')"
).fetchone()[0]
conn.close()
print(n)
