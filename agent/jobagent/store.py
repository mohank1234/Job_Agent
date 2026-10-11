"""Durable job store.

Design rule: **every fetched job is retained.** Nothing is filtered out before
storage and nothing is ever deleted. A posting judged irrelevant is stored with
match_category 'E' — it stays queryable forever.

The table is still called `seen` so existing databases keep their history;
missing columns are added in place on open.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Job

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    fingerprint TEXT PRIMARY KEY,
    company     TEXT,
    title       TEXT,
    url         TEXT,
    source      TEXT,
    score       INTEGER,
    verdict     TEXT,
    first_seen  TEXT,
    reported    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_seen_first ON seen(first_seen);

-- Tombstones for jobs that were judged irrelevant or went stale and were
-- pruned out of `seen`. Deliberately tiny: no description, no raw payload.
-- Its only job is to stop a pruned posting being re-fetched, re-classified
-- and re-scored by the LLM on every subsequent run.
CREATE TABLE IF NOT EXISTS dismissed (
    fingerprint    TEXT PRIMARY KEY,
    company        TEXT,
    title          TEXT,
    content_hash   TEXT,
    match_category TEXT,
    score          INTEGER,
    reason         TEXT,
    dismissed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_dismissed_hash ON dismissed(content_hash);
"""

# Added to existing databases with ALTER TABLE — old rows keep their history.
EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("source_type",          "TEXT"),
    ("location",             "TEXT"),
    ("workplace",            "TEXT"),
    ("description",          "TEXT"),
    ("salary",               "TEXT"),
    ("posted_at",            "TEXT"),
    ("raw",                  "TEXT"),
    ("role_family",          "TEXT"),
    ("role_label",           "TEXT"),
    ("role_tier",            "INTEGER"),
    ("seniority",            "TEXT"),
    ("base_score",           "INTEGER"),
    ("candidate",            "INTEGER DEFAULT 0"),
    ("evidence",             "TEXT"),
    ("gap_terms",            "TEXT"),
    ("years_required",       "REAL"),
    ("classification_notes", "TEXT"),
    ("match_category",       "TEXT"),
    ("match_category_label", "TEXT"),
    ("priority",             "TEXT"),
    ("why_matches",          "TEXT"),
    ("matching_skills",      "TEXT"),
    ("missing_skills",       "TEXT"),
    ("experience_gap",       "TEXT"),
    ("recommendation",       "TEXT"),
    ("scored_by",            "TEXT"),
    ("content_hash",         "TEXT"),
    ("last_seen",            "TEXT"),
    ("last_scored",          "TEXT"),
    # The DATE a job was first put in a digest. `reported` alone cannot tell
    # "shown today" from "shown last week", and the difference matters: a
    # second run on the same day must keep the morning's jobs in today's file,
    # while tomorrow's file must drop them.
    ("reported_on",          "TEXT"),
]

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_seen_score ON seen(score)",
    "CREATE INDEX IF NOT EXISTS idx_seen_cat   ON seen(match_category)",
    "CREATE INDEX IF NOT EXISTS idx_seen_fam   ON seen(role_family)",
    "CREATE INDEX IF NOT EXISTS idx_seen_last  ON seen(last_seen)",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return "[]"


def _loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


class Store:
    def __init__(self, path: str | Path = "jobs.db", store_raw: bool = False):
        # The raw source payload duplicates description + the parsed columns and
        # was 53% of the database (50 MB of 95 MB) for no read path. Off by
        # default; set retention.store_raw to keep it.
        self.store_raw = store_raw
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(BASE_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(seen)")}
        for name, decl in EXTRA_COLUMNS:
            if name not in have:
                self.conn.execute(f"ALTER TABLE seen ADD COLUMN {name} {decl}")
        for stmt in INDEXES:
            self.conn.execute(stmt)

    # ------------------------------------------------------------ retention
    def upsert_all(self, jobs: list[Job], policy_version: str = "") -> tuple[int, int]:
        """Store every job. Returns (newly_seen, already_known).

        first_seen is preserved across runs; last_seen always advances.

        The deterministic match (score / category / priority) is written for
        EVERY job, so nothing sits in the database uncategorised — including
        the postings that will never be sent to the LLM. An existing LLM
        verdict on unchanged content is never overwritten by a rules verdict.

        `policy_version` must be the same value the scoring pass will use in
        `cached_match`/`apply_cached` (matcher.policy_version(profile,
        llm_cfg)) — the ON CONFLICT clause below decides whether to keep an
        existing LLM verdict by comparing stored vs. freshly-computed
        content_hash, and both hashes must be computed under the same policy
        for that comparison to mean anything.
        """
        now = _now()
        new = known = 0
        for job in jobs:
            row = self.conn.execute(
                "SELECT first_seen FROM seen WHERE fingerprint = ?", (job.fingerprint,)
            ).fetchone()
            first_seen = row["first_seen"] if row and row["first_seen"] else now
            if row:
                known += 1
            else:
                new += 1

            self.conn.execute(
                """INSERT INTO seen (
                       fingerprint, company, title, url, source, source_type,
                       location, workplace, description, salary, posted_at, raw,
                       role_family, role_label, role_tier, seniority, base_score,
                       candidate, evidence, gap_terms, years_required,
                       classification_notes, content_hash, first_seen, last_seen,
                       score, verdict, match_category, match_category_label,
                       priority, why_matches, matching_skills, missing_skills,
                       experience_gap, recommendation, scored_by
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                             ?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                       company=excluded.company,
                       title=excluded.title,
                       url=excluded.url,
                       source=excluded.source,
                       source_type=excluded.source_type,
                       location=excluded.location,
                       workplace=excluded.workplace,
                       description=excluded.description,
                       salary=excluded.salary,
                       posted_at=excluded.posted_at,
                       raw=excluded.raw,
                       role_family=excluded.role_family,
                       role_label=excluded.role_label,
                       role_tier=excluded.role_tier,
                       seniority=excluded.seniority,
                       base_score=excluded.base_score,
                       candidate=excluded.candidate,
                       evidence=excluded.evidence,
                       gap_terms=excluded.gap_terms,
                       years_required=excluded.years_required,
                       classification_notes=excluded.classification_notes,
                       content_hash=excluded.content_hash,
                       last_seen=excluded.last_seen,
                       -- Keep an existing LLM verdict when the posting has not
                       -- materially changed; otherwise refresh with the new
                       -- deterministic verdict.
                       score=CASE WHEN seen.scored_by='llm'
                                   AND seen.content_hash=excluded.content_hash
                                  THEN seen.score ELSE excluded.score END,
                       verdict=CASE WHEN seen.scored_by='llm'
                                     AND seen.content_hash=excluded.content_hash
                                    THEN seen.verdict ELSE excluded.verdict END,
                       match_category=CASE WHEN seen.scored_by='llm'
                                            AND seen.content_hash=excluded.content_hash
                                           THEN seen.match_category
                                           ELSE excluded.match_category END,
                       match_category_label=CASE WHEN seen.scored_by='llm'
                                                  AND seen.content_hash=excluded.content_hash
                                                 THEN seen.match_category_label
                                                 ELSE excluded.match_category_label END,
                       priority=CASE WHEN seen.scored_by='llm'
                                      AND seen.content_hash=excluded.content_hash
                                     THEN seen.priority ELSE excluded.priority END,
                       why_matches=CASE WHEN seen.scored_by='llm'
                                         AND seen.content_hash=excluded.content_hash
                                        THEN seen.why_matches ELSE excluded.why_matches END,
                       matching_skills=CASE WHEN seen.scored_by='llm'
                                             AND seen.content_hash=excluded.content_hash
                                            THEN seen.matching_skills
                                            ELSE excluded.matching_skills END,
                       missing_skills=CASE WHEN seen.scored_by='llm'
                                            AND seen.content_hash=excluded.content_hash
                                           THEN seen.missing_skills
                                           ELSE excluded.missing_skills END,
                       experience_gap=CASE WHEN seen.scored_by='llm'
                                            AND seen.content_hash=excluded.content_hash
                                           THEN seen.experience_gap
                                           ELSE excluded.experience_gap END,
                       recommendation=CASE WHEN seen.scored_by='llm'
                                            AND seen.content_hash=excluded.content_hash
                                           THEN seen.recommendation
                                           ELSE excluded.recommendation END,
                       scored_by=CASE WHEN seen.scored_by='llm'
                                       AND seen.content_hash=excluded.content_hash
                                      THEN seen.scored_by ELSE excluded.scored_by END
                """,
                (
                    job.fingerprint, job.company, job.title, job.url, job.source,
                    job.source_type, job.location, job.workplace, job.description,
                    job.salary,
                    job.posted_at.isoformat() if job.posted_at else None,
                    job.raw_json() if self.store_raw else None,
                    job.role_family, job.role_label, job.role_tier, job.seniority,
                    job.base_score, int(job.candidate), _dumps(job.evidence),
                    _dumps(job.gap_terms), job.years_required,
                    _dumps(job.classification_notes), job.content_hash(policy_version),
                    first_seen, now,
                    job.score, job.match_category, job.match_category,
                    job.match_category_label, job.priority,
                    _dumps(job.why_matches), _dumps(job.matching_skills),
                    _dumps(job.missing_skills), job.experience_gap,
                    job.recommendation, job.scored_by or "rules",
                ),
            )
        self.conn.commit()
        return new, known

    # -------------------------------------------------------------- scoring
    def cached_match(self, job: Job, policy_version: str = "") -> dict | None:
        """Previous match for an UNCHANGED posting under the SAME matching
        policy, so we never pay to rescore something that hasn't materially
        changed — and never silently reuse a verdict computed under an old
        profile/prompt/model (repo audit 2026-09-07 finding #3)."""
        row = self.conn.execute(
            """SELECT score, match_category, match_category_label, priority,
                      why_matches, matching_skills, missing_skills,
                      experience_gap, recommendation, scored_by, content_hash,
                      last_scored
               FROM seen WHERE fingerprint = ?""",
            (job.fingerprint,),
        ).fetchone()
        if not row or not row["last_scored"]:
            return None
        if row["content_hash"] != job.content_hash(policy_version):
            return None                      # description or policy changed -> rescore
        if (row["scored_by"] or "") not in ("llm", "cache"):
            return None                      # only LLM verdicts are worth reusing
        return dict(row)

    def apply_cached(self, job: Job, policy_version: str = "") -> bool:
        cached = self.cached_match(job, policy_version)
        if not cached:
            return False
        job.score = cached["score"] or 0
        job.match_category = cached["match_category"] or ""
        job.match_category_label = cached["match_category_label"] or ""
        job.priority = cached["priority"] or ""
        job.why_matches = _loads(cached["why_matches"], [])
        job.matching_skills = _loads(cached["matching_skills"], [])
        job.missing_skills = _loads(cached["missing_skills"], [])
        job.experience_gap = cached["experience_gap"] or ""
        job.recommendation = cached["recommendation"] or ""
        job.scored_by = "cache"
        return True

    def record_match(self, job: Job, reported: bool | None = None, *, commit=True) -> None:
        fields = [
            "score=?", "verdict=?", "match_category=?", "match_category_label=?",
            "priority=?", "why_matches=?", "matching_skills=?", "missing_skills=?",
            "experience_gap=?", "recommendation=?", "scored_by=?", "last_scored=?",
        ]
        values = [
            job.score, job.match_category, job.match_category,
            job.match_category_label, job.priority, _dumps(job.why_matches),
            _dumps(job.matching_skills), _dumps(job.missing_skills),
            job.experience_gap, job.recommendation, job.scored_by, _now(),
        ]
        if reported is not None:
            fields.append("reported=?")
            values.append(int(reported))
        values.append(job.fingerprint)
        self.conn.execute(
            f"UPDATE seen SET {', '.join(fields)} WHERE fingerprint = ?", values
        )
        if commit:
            self.conn.commit()

    def record_classification(self, job: Job, policy_version: str = "", *, commit=True) -> None:
        """Persist the stage-1 classification fields (role_family, candidate,
        base_score, etc.) plus content_hash.

        `record_match` alone does NOT write these — it only persists the
        stage-2 match fields (score/category/priority/...). `rescore`
        recomputes classification for stored rows outside the normal
        fetch -> upsert_all path, and calling only `record_match` after that
        left role_family/candidate/base_score permanently stale even though
        match_category had updated (repo audit 2026-09-07 finding #13,
        reproduced against a throwaway db). Call this alongside
        `record_match` whenever classification was recomputed for an
        already-stored row.
        """
        self.conn.execute(
            """UPDATE seen SET
                   role_family=?, role_label=?, role_tier=?, seniority=?,
                   base_score=?, candidate=?, evidence=?, gap_terms=?,
                   years_required=?, classification_notes=?, content_hash=?
               WHERE fingerprint = ?""",
            (
                job.role_family, job.role_label, job.role_tier, job.seniority,
                job.base_score, int(job.candidate), _dumps(job.evidence),
                _dumps(job.gap_terms), job.years_required,
                _dumps(job.classification_notes), job.content_hash(policy_version),
                job.fingerprint,
            ),
        )
        if commit:
            self.conn.commit()

    def record_evaluation(self, job: Job, policy_version: str) -> None:
        """Never expose a new policy hash paired with an old match after a crash."""
        with self.conn:
            self.record_classification(job, policy_version, commit=False)
            self.record_match(job, commit=False)

    def already_reported(self, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT reported FROM seen WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return bool(row and row["reported"])

    # ---------------------------------------------------------------- prune
    def dismissed_hashes(self) -> dict[str, str]:
        """fingerprint -> content_hash for everything already rejected."""
        return {
            r["fingerprint"]: r["content_hash"] or ""
            for r in self.conn.execute(
                "SELECT fingerprint, content_hash FROM dismissed"
            )
        }

    def prune(self, categories=("D", "E"), max_age_days: float | None = None) -> dict:
        """Delete jobs that do not fit, and jobs that have gone stale.

        Each deleted row leaves a tombstone in `dismissed` so it is skipped
        cheaply next run instead of being re-scored. Returns the counts.
        """
        now = datetime.now(timezone.utc)
        marks = ",".join("?" for _ in categories)
        doomed: list[tuple[str, str]] = []       # (fingerprint, reason)

        # Only delete a non-match we are actually sure about: either the LLM
        # judged it, or the classifier never treated it as a candidate at all.
        # A candidate still waiting for its LLM verdict must survive — pruning
        # it on a deterministic guess would silently discard a real match.
        for r in self.conn.execute(
            f"""SELECT fingerprint FROM seen
                WHERE match_category IN ({marks})
                  AND (scored_by = 'llm' OR candidate = 0)""",
            tuple(categories),
        ):
            doomed.append((r["fingerprint"], "not a match"))

        if max_age_days is not None:
            for r in self.conn.execute(
                "SELECT fingerprint, posted_at FROM seen WHERE posted_at IS NOT NULL"
            ):
                try:
                    posted = datetime.fromisoformat(r["posted_at"])
                except (TypeError, ValueError):
                    continue
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
                if (now - posted).total_seconds() / 86400 > max_age_days:
                    doomed.append((r["fingerprint"], "stale"))

        seen_fps: set[str] = set()
        stats = {"not a match": 0, "stale": 0}
        for fingerprint, reason in doomed:
            if fingerprint in seen_fps:
                continue
            seen_fps.add(fingerprint)
            row = self.conn.execute(
                """SELECT company, title, content_hash, match_category, score
                   FROM seen WHERE fingerprint = ?""",
                (fingerprint,),
            ).fetchone()
            if not row:
                continue
            self.conn.execute(
                """INSERT INTO dismissed (fingerprint, company, title, content_hash,
                                          match_category, score, reason, dismissed_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                       content_hash=excluded.content_hash,
                       match_category=excluded.match_category,
                       score=excluded.score,
                       reason=excluded.reason,
                       dismissed_at=excluded.dismissed_at""",
                (fingerprint, row["company"], row["title"], row["content_hash"],
                 row["match_category"], row["score"], reason, _now()),
            )
            self.conn.execute("DELETE FROM seen WHERE fingerprint = ?", (fingerprint,))
            stats[reason] += 1

        self.conn.commit()
        self.conn.execute("VACUUM")           # actually give the disk space back
        stats["total"] = stats["not a match"] + stats["stale"]
        return stats

    def is_new(self, job: Job) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen WHERE fingerprint = ?", (job.fingerprint,)
        ).fetchone()
        return row is None

    # ----------------------------------------------------------------- read
    def stats(self) -> dict:
        c = self.conn
        total = c.execute("SELECT COUNT(*) n FROM seen").fetchone()["n"]
        reported = c.execute("SELECT COUNT(*) n FROM seen WHERE reported=1").fetchone()["n"]
        scored = c.execute("SELECT COUNT(*) n FROM seen WHERE last_scored IS NOT NULL").fetchone()["n"]
        cands = c.execute("SELECT COUNT(*) n FROM seen WHERE candidate=1").fetchone()["n"]
        by_cat = {
            r["match_category"] or "-": r["n"]
            for r in c.execute(
                "SELECT match_category, COUNT(*) n FROM seen GROUP BY match_category"
            )
        }
        by_family = {
            r["role_family"] or "-": r["n"]
            for r in c.execute(
                "SELECT role_family, COUNT(*) n FROM seen GROUP BY role_family "
                "ORDER BY n DESC"
            )
        }
        by_source = {
            r["source"] or "-": r["n"]
            for r in c.execute(
                "SELECT source, COUNT(*) n FROM seen GROUP BY source ORDER BY n DESC"
            )
        }
        dismissed = c.execute("SELECT COUNT(*) n FROM dismissed").fetchone()["n"]
        return {
            "total_seen": total,
            "reported": reported,
            "scored": scored,
            "candidates": cands,
            "dismissed": dismissed,
            "by_category": by_cat,
            "by_family": by_family,
            "by_source": by_source,
        }

    def top(self, limit: int = 25, categories: tuple[str, ...] = ("A", "B", "C"),
            only_new: bool = False, today: str | None = None,
            max_age_days: float | None = None) -> list[dict]:
        """Best matches. With only_new, a job that appeared in an EARLIER day's
        digest is excluded, so every day's file contains jobs you have not seen.

        Jobs reported *today* are still returned: a second run on the same day
        rewrites the same file, and must not empty out what the morning found.
        """
        marks = ",".join("?" for _ in categories)
        where = [f"match_category IN ({marks})"]
        params: list = list(categories)
        if only_new:
            today = today or datetime.now().strftime("%Y-%m-%d")
            where.append(
                "(reported IS NULL OR reported = 0 "
                " OR reported_on IS NULL OR reported_on = ?)"
            )
            params.append(today)
        if max_age_days is not None:
            where.append("(posted_at IS NULL OR julianday(posted_at) IS NULL "
                         "OR julianday('now') - julianday(posted_at) <= ?)")
            params.append(float(max_age_days))
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT * FROM seen
                WHERE {' AND '.join(where)}
                ORDER BY score DESC, base_score DESC LIMIT ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_reported(self, fingerprints, today: str | None = None) -> None:
        """Record that these jobs went into today's digest."""
        today = today or datetime.now().strftime("%Y-%m-%d")
        for fp in fingerprints:
            self.conn.execute(
                "UPDATE seen SET reported = 1, "
                "reported_on = COALESCE(reported_on, ?) WHERE fingerprint = ?",
                (today, fp),
            )
        self.conn.commit()

    def unscored_candidates(self, limit: int = 1000) -> list[dict]:
        """Candidates still waiting for an LLM verdict — this is what makes an
        interrupted run resumable."""
        rows = self.conn.execute(
            """SELECT * FROM seen
               WHERE candidate=1 AND (scored_by IS NULL OR scored_by != 'llm')
               ORDER BY base_score DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def compact(self) -> tuple[float, float]:
        """Reclaim space from dropped raw payloads. Returns (MB before, after)."""
        import os
        path = self.conn.execute("PRAGMA database_list").fetchone()[2]
        before = os.path.getsize(path) / 1e6
        self.conn.execute("UPDATE seen SET raw = NULL") if not self.store_raw else None
        self.conn.commit()
        self.conn.execute("VACUUM")
        return before, os.path.getsize(path) / 1e6

    def close(self) -> None:
        self.conn.close()


def row_to_job(row: dict) -> Job:
    """Rebuild a Job from a stored row (used by resume + digest paths)."""
    posted = row.get("posted_at")
    parsed = None
    if posted:
        try:
            parsed = datetime.fromisoformat(posted)
        except (TypeError, ValueError):
            parsed = None
    job = Job(
        source=row.get("source") or "",
        company=row.get("company") or "",
        title=row.get("title") or "",
        url=row.get("url") or "",
        location=row.get("location") or "",
        workplace=row.get("workplace") or "unknown",
        description=row.get("description") or "",
        salary=row.get("salary") or "",
        posted_at=parsed,
    )
    job.role_family = row.get("role_family") or ""
    job.role_label = row.get("role_label") or ""
    job.role_tier = row.get("role_tier") or 4
    job.seniority = row.get("seniority") or ""
    job.base_score = row.get("base_score") or 0
    job.candidate = bool(row.get("candidate"))
    job.evidence = _loads(row.get("evidence"), {})
    job.gap_terms = _loads(row.get("gap_terms"), [])
    job.years_required = row.get("years_required")
    job.classification_notes = _loads(row.get("classification_notes"), [])
    job.score = row.get("score") or 0
    job.match_category = row.get("match_category") or ""
    job.match_category_label = row.get("match_category_label") or ""
    job.priority = row.get("priority") or ""
    job.why_matches = _loads(row.get("why_matches"), [])
    job.matching_skills = _loads(row.get("matching_skills"), [])
    job.missing_skills = _loads(row.get("missing_skills"), [])
    job.experience_gap = row.get("experience_gap") or ""
    job.recommendation = row.get("recommendation") or ""
    job.scored_by = row.get("scored_by") or ""
    return job
