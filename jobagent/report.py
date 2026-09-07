"""Daily digest.

Only A / B / C matches inside the freshness window are shown, newest first —
a role answered on day one sits near the front of the recruiter's pile.
Jobs that do not fit are pruned from the database after matching.
"""

from __future__ import annotations

from datetime import datetime

from .models import Job

# Freshness leads the digest: a role you answer on day one is worth more than
# the same role on day six, because the pile the recruiter is reading is
# smaller. Fit still decides ordering WITHIN each window.
FRESHNESS_SECTIONS = [
    (0, "Posted in the last 24 hours", "Apply today — you are near the front of the queue."),
    (1, "Posted in the last 2 days", "Still early. Apply now."),
    (2, "Posted in the last 4 days", "Worth applying; the pile is filling up."),
    (3, "Posted this week", "Last of the useful window."),
    (9, "Posting date not published", "The board did not state a date — check the listing."),
]
CATEGORY_ORDER = {"A": 0, "B": 1, "C": 2}
CATEGORY_LABEL = {"A": "A · strong", "B": "B · very good", "C": "C · good/adjacent"}
PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "": 3}


def _fmt_list(items: list[str], empty: str = "—") -> str:
    clean = [i for i in items if i]
    return "; ".join(clean) if clean else empty


def build_markdown(
    jobs: list[Job],
    stats: dict | None = None,
    notes: list[str] | None = None,
    new_fingerprints: set[str] | None = None,
) -> str:
    notes = notes or []
    new_fingerprints = new_fingerprints or set()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    fits = [j for j in jobs if j.match_category in CATEGORY_ORDER]
    buckets: dict[int, list[Job]] = {}
    for job in fits:
        buckets.setdefault(job.freshness[0], []).append(job)
    for bucket in buckets.values():
        bucket.sort(key=lambda j: (
            CATEGORY_ORDER.get(j.match_category, 3),
            PRIORITY_ORDER.get(j.priority, 3),
            -j.score,
        ))

    shown = len(fits)
    by_cat = {c: sum(1 for j in fits if j.match_category == c) for c in "ABC"}
    lines = [
        f"# Job digest — {stamp}",
        "",
        f"**{shown}** jobs worth your attention "
        f"(A: {by_cat['A']} · B: {by_cat['B']} · C: {by_cat['C']}).",
        "",
        f"Newest first: **{len(buckets.get(0, []))}** posted in the last 24 hours, "
        f"**{len(buckets.get(1, []))}** in the last 2 days, "
        f"**{len(buckets.get(2, []))}** in the last 4 days.",
        "",
    ]

    if stats:
        # `run.py digest` rebuilds from the database with no fetch counters, so
        # only render the rows this invocation actually knows.
        rows: list[tuple[str, str]] = [
            ("Jobs in database", stats.get("total_seen")),
            ("Candidates identified", stats.get("candidates")),
            ("Scored", stats.get("scored")),
        ]
        if stats.get("fetched") is not None:
            rows += [
                ("Fetched this run",
                 f"{stats['fetched']} (ATS {stats.get('ats', 0)} · "
                 f"aggregators {stats.get('aggregator', 0)})"),
                ("Unique after dedupe", stats.get("unique")),
                ("New since last run", stats.get("new")),
            ]
        if stats.get("llm") is not None:
            rows.append((
                "Scored by LLM this run",
                f"{stats['llm']} (cached {stats.get('cached', 0)}, "
                f"fallback {stats.get('fallback', 0)})",
            ))
        if stats.get("model"):
            via = stats.get("provider")
            # "(free)" used to be asserted unconditionally, derived only from
            # the provider NAME being on config.yaml's trusted allow-list —
            # no token count or billing response is ever read anywhere in
            # llm.py/matcher.py/report.py (repo audit 2026-09-07 finding
            # #16). This is honest about what's actually known: the provider
            # is configured as free-tier, not that usage was measured as
            # zero cost.
            rows.append((
                "Model",
                f"`{stats['model']}`"
                + (f" via {via} (configured as free-tier)" if via
                   else " (configured as free-tier)"),
            ))

        lines += ["| | |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in rows if v is not None]
        lines.append("")

    if not shown:
        lines += [
            "No A/B/C matches inside the freshness window this run. Widen "
            "`retention.max_age_days` in config.yaml, or add companies to "
            "`companies.yaml`. Check `python run.py stats` for what was seen.",
            "",
        ]

    for rank, heading, blurb in FRESHNESS_SECTIONS:
        bucket = buckets.get(rank) or []
        if not bucket:
            continue
        lines += [f"## {heading} ({len(bucket)})", "", f"_{blurb}_", ""]
        for job in bucket:
            age = job.age_days
            if age is None:
                age_str = "date unknown"
            elif age < 1:
                age_str = f"{age * 24:.0f}h ago"
            else:
                age_str = f"{age:.0f}d ago"
            flag = " · **NEW**" if job.fingerprint in new_fingerprints else ""
            lines += [
                f"### [{job.title} — {job.company}]({job.url})",
                "",
                f"**Match {job.score}/100** · "
                f"{CATEGORY_LABEL.get(job.match_category, job.match_category)} · "
                f"**{job.priority or 'LOW'} PRIORITY** · posted {age_str}{flag}",
                "",
                f"- **Role family:** {job.role_label} ({job.seniority} level)",
                f"- **Location:** {job.location or 'n/a'} · {job.workplace}"
                + (f" · {job.salary}" if job.salary else ""),
                f"- **Source:** {job.source} ({job.source_type})",
                f"- **Why it matches:** {_fmt_list(job.why_matches)}",
                f"- **Matching skills:** {_fmt_list(job.matching_skills)}",
                f"- **Missing skills:** {_fmt_list(job.missing_skills, 'none identified')}",
                f"- **Experience / seniority gap:** {job.experience_gap or 'none'}",
            ]
            if job.years_required:
                lines.append(f"- **Stated requirement:** ~{job.years_required:.0f} years")
            lines += [
                f"- **Recommendation:** {job.recommendation or '—'}",
                f"- **Scored by:** {job.scored_by}",
                "",
            ]

    if notes:
        lines += ["---", "", "## Run notes", ""] + [f"- {n}" for n in notes] + [""]

    footer = (
        "_Only postings from the last "
        f"{stats.get('window_days')} days are considered._ "
        if stats and stats.get("window_days") else
        "_Only recent postings are considered._ "
    )
    if stats and stats.get("pruned") is not None:
        footer += (
            f"_{stats['pruned']} non-matching or stale jobs were pruned this "
            f"run; a tombstone keeps them from ever being re-scored._"
        )
    lines += ["---", "", footer.strip(), ""]
    return "\n".join(lines)
