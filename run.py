#!/usr/bin/env python
"""Personal job discovery + matching agent.

    python run.py fetch            full run  -> digest_YYYY-MM-DD.md
    python run.py fetch --no-llm   classify + store only, zero LLM calls
    python run.py fetch --limit N  cap LLM scoring this run (resumable)
    python run.py score            resume LLM scoring of stored candidates
    python run.py digest           rebuild the digest from jobs.db
    python run.py stats            what the database holds
    python run.py top              best matches currently in the database
    python run.py classify-test    role-classification self-test
    python run.py verify           check every ATS slug in companies.yaml
    python run.py tailor <url>     write a tailored pitch for one job
    python run.py models           browse OpenRouter's public model catalogue

Pipeline:
    sources -> normalize -> dedupe -> freshness window -> store ->
    role classification -> candidate detection -> profile matching (LLM) ->
    rank -> prune -> digest

Only postings inside `retention.max_age_days` are considered. After matching,
jobs that do not fit (category D/E) and jobs that have gone stale are deleted,
leaving a small tombstone (fingerprint + content hash + category) so they are
never re-fetched into the working set or re-scored by the LLM.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from jobagent.llm import list_openrouter_models
from jobagent.matcher import (rule_match, rule_match_all,
                              score_candidates, tailor)
from jobagent.models import Job
from jobagent.profile import load_profile
from jobagent.report import build_markdown
from jobagent.roles import candidates as pick_candidates
from jobagent.roles import classify, classify_all
from jobagent.sources import fetch_ats, fetch_feeds, verify_boards
from jobagent.store import Store, row_to_job

ROOT = Path(__file__).parent

# Windows consoles still default to cp1252, which cannot encode the spinner and
# bullet glyphs rich emits — that crashes the run mid-fetch. Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

console = Console(legacy_windows=False)


def load_yaml(name: str) -> dict:
    path = ROOT / name
    if not path.exists():
        console.print(f"[red]Missing {name}[/red]")
        sys.exit(1)
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def dedupe(jobs: list[Job]) -> list[Job]:
    """Collapse the same job seen via several sources. ATS wins — it's canonical.
    The surviving record keeps the richest description available."""
    priority = {
        "greenhouse": 0, "lever": 0, "ashby": 0, "smartrecruiters": 0, "workable": 0,
    }
    best: dict[str, Job] = {}
    for job in jobs:
        key = job.fingerprint
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = job
            continue
        if priority.get(job.source, 5) < priority.get(incumbent.source, 5):
            best[key] = job
        elif len(job.description) > len(incumbent.description) * 1.5:
            best[key] = job
    return list(best.values())


def collect(config: dict) -> tuple[list[Job], list[str], dict]:
    sources = config.get("sources", {})
    notes: list[str] = []
    jobs: list[Job] = []
    counts = {"ats": 0, "aggregator": 0}

    if sources.get("ats", True):
        boards = load_yaml("companies.yaml")
        with console.status("Fetching company ATS boards..."):
            ats_jobs = fetch_ats(boards)
        counts["ats"] = len(ats_jobs)
        console.print(f"  ATS boards       : {len(ats_jobs)} postings")
        jobs += ats_jobs

    with console.status("Fetching aggregator feeds..."):
        feed_jobs, feed_errors = fetch_feeds(sources)
    counts["aggregator"] = len(feed_jobs)
    console.print(f"  Aggregator feeds : {len(feed_jobs)} postings")
    jobs += feed_jobs
    notes += feed_errors

    return jobs, notes, counts


# ---------------------------------------------------------------------------
# fetch — the full pipeline
# ---------------------------------------------------------------------------

def cmd_fetch(args) -> None:
    config = load_yaml("config.yaml")
    llm_cfg = config.get("llm", {})
    profile = load_profile(ROOT / config.get("profile_file", "profile.yaml"))

    console.print(f"[bold]Profile[/bold]: {profile.headline}\n")

    console.print("[bold]1. Collecting[/bold]")
    jobs, notes, counts = collect(config)
    fetched = len(jobs)

    jobs = dedupe(jobs)
    console.print(f"  After dedupe     : {len(jobs)} unique")

    # Freshness. EVERY job is stored regardless — a posting that is stale today
    # is still the record of what was on the market. The window only decides
    # what the digest shows (digest.max_age_days).
    retention = config.get("retention", {})
    digest_window = config.get("digest", {}).get("max_age_days")
    max_age = retention.get("max_age_days")
    if max_age:
        stale = sum(
            1 for j in jobs
            if j.age_days is not None and j.age_days > float(max_age)
        )
        console.print(
            f"  [yellow]retention.max_age_days={max_age} would discard {stale} "
            f"postings before storage, which breaks the retain-everything "
            f"guarantee. Set it to null.[/yellow]"
        )
    console.print()

    console.print("[bold]2. Classifying roles[/bold] (free, offline)")
    classify_all(jobs, profile)
    rule_match_all(jobs, profile)        # every job leaves here with a score

    store = Store(ROOT / "jobs.db")

    # Drop anything already judged irrelevant and unchanged since. Pruned rows
    # are gone from `seen`, so the score cache cannot help them — without this
    # check every rejected posting would be re-stored and re-scored by the LLM
    # on every run. The tombstone hash includes the classification, so a job
    # DOES come back for reconsideration if the classifier or the posting
    # changes.
    tombstones = store.dismissed_hashes()
    if tombstones:
        kept = [j for j in jobs if tombstones.get(j.fingerprint) != j.content_hash]
        skipped = len(jobs) - len(kept)
        if skipped:
            console.print(
                f"  Previously rejected: {skipped} skipped (unchanged since dismissal)"
            )
        jobs = kept

    cands = pick_candidates(jobs, qa_only=profile.qa_roles_only)

    # FRESHNESS WINDOW. Older postings stay in the database and keep their
    # deterministic score, but they are not worth an LLM call or a slot in the
    # digest: the queue in front of you is already long.
    window = config.get("digest", {}).get("max_age_days")
    if window:
        fresh = [j for j in cands
                 if j.age_days is None or j.age_days <= float(window)]
        if len(fresh) != len(cands):
            console.print(
                f"  {len(cands) - len(fresh)} candidates older than {window}d "
                f"kept in the database but not scored"
            )
        cands = fresh
    by_family: dict[str, int] = {}
    for job in jobs:
        by_family[job.role_label] = by_family.get(job.role_label, 0) + 1
    for label, n in sorted(by_family.items(), key=lambda kv: -kv[1])[:10]:
        console.print(f"  {n:5}  {label}")
    console.print(f"  [bold]{len(cands)} candidates[/bold] for detailed matching\n")

    console.print("[bold]3. Storing[/bold]")
    new_count, known = store.upsert_all(jobs)
    console.print(f"  Stored           : {len(jobs)} ({new_count} new, {known} updated)\n")

    # A job first seen in this run has first_seen == last_seen.
    new_fps: set[str] = set()
    for job in jobs:
        row = store.conn.execute(
            "SELECT first_seen, last_seen FROM seen WHERE fingerprint=?",
            (job.fingerprint,),
        ).fetchone()
        if row and row["first_seen"] == row["last_seen"]:
            new_fps.add(job.fingerprint)

    scored_counts = {"cached": 0, "llm": 0, "fallback": 0}
    if args.no_llm:
        console.print("[yellow]--no-llm: keeping deterministic scores only.[/yellow]\n")
        for job in cands:
            store.record_match(job)
    else:
        cap = args.limit or int(llm_cfg.get("max_jobs_per_run", 60))
        todo = cands[:cap]
        console.print(
            f"[bold]4. Matching against profile[/bold] — {len(todo)} of "
            f"{len(cands)} candidates via {llm_cfg.get('provider')} / "
            f"{llm_cfg.get('model')}"
        )
        if len(cands) > len(todo):
            console.print(
                f"  [dim]{len(cands) - len(todo)} candidates deferred — "
                f"`python run.py score` resumes them.[/dim]"
            )
        # Candidates the LLM will not reach this run still need their
        # deterministic verdict persisted.
        for job in cands[len(todo):]:
            store.record_match(job)

        with console.status("Matching...") as status:
            def progress(done, total):
                status.update(f"Matching... {done}/{total}")
            errors, scored_counts = score_candidates(
                todo, profile, llm_cfg, store=store, on_progress=progress
            )
        notes += errors
        console.print(
            f"  LLM-scored {scored_counts['llm']} · cached {scored_counts['cached']} "
            f"· rules fallback {scored_counts['fallback']}\n"
        )

    pruned = {}
    if retention.get("prune_non_matching", False):
        console.print("[bold]5. Pruning[/bold]")
        pruned = store.prune(
            categories=tuple(retention.get("prune_categories") or ("D", "E")),
            max_age_days=float(max_age) if max_age else None,
        )
        console.print(
            f"  Removed {pruned['total']} "
            f"({pruned['not a match']} not a match, {pruned['stale']} stale) — "
            f"tombstoned so they are never re-scored\n"
        )

    _write_digest(store, notes, new_fps, {
        "fetched": fetched,
        "ats": counts["ats"],
        "aggregator": counts["aggregator"],
        "unique": len(jobs),
        "new": new_count,
        "candidates": len(cands),
        "pruned": pruned.get("total"),
        "window_days": max_age,
        **scored_counts,
    }, max_age_days=digest_window,
       only_new=config.get("digest", {}).get("only_new", True))
    store.close()


def _write_digest(store: Store, notes, new_fps, extra_stats, max_age_days=None,
                  only_new: bool = True) -> None:
    """Write today's digest.

    With only_new (the default), a job that appeared in an earlier day's file
    is never shown again — each day's digest is only what you have not already
    seen. An empty digest is a valid, honest result. Jobs already reported
    TODAY are still included, so the evening catch-up run does not erase what
    the morning run found.
    """
    stats = store.stats()
    stats.update(extra_stats)
    rows = store.top(limit=120, categories=("A", "B", "C"), only_new=only_new)
    jobs = [row_to_job(r) for r in rows]

    # Age only gates the DIGEST. Every job stays in the database.
    if max_age_days:
        keep = [
            (r, j) for r, j in zip(rows, jobs)
            if j.age_days is None or j.age_days <= max_age_days
        ]
        rows = [r for r, _ in keep]
        jobs = [j for _, j in keep]
    rows, jobs = rows[:60], jobs[:60]

    fingerprints = {r["fingerprint"] for r in rows if r["fingerprint"] in new_fps}

    markdown = build_markdown(jobs, stats=stats, notes=notes,
                              new_fingerprints=fingerprints)
    out = ROOT / f"digest_{datetime.now():%Y-%m-%d}.md"
    out.write_text(markdown, encoding="utf-8")

    store.mark_reported(r["fingerprint"] for r in rows)

    console.print(f"[bold]6. Digest[/bold] -> {out}")
    by_cat: dict[str, int] = {}
    for job in jobs:
        by_cat[job.match_category] = by_cat.get(job.match_category, 0) + 1
    console.print(
        f"  A {by_cat.get('A', 0)} · B {by_cat.get('B', 0)} · C {by_cat.get('C', 0)}\n"
    )
    for job in jobs[:10]:
        console.print(
            f"  [bold]{job.score:3}[/bold]  {job.match_category}  "
            f"{job.priority:6}  {job.title[:56]} — {job.company}"
        )
    for note in notes[:8]:
        console.print(f"  [yellow]note:[/yellow] {note}")


# ---------------------------------------------------------------------------
# score — resume LLM scoring without refetching
# ---------------------------------------------------------------------------

def cmd_score(args) -> None:
    config = load_yaml("config.yaml")
    llm_cfg = config.get("llm", {})
    profile = load_profile(ROOT / config.get("profile_file", "profile.yaml"))
    store = Store(ROOT / "jobs.db")

    cap = args.limit or int(llm_cfg.get("max_jobs_per_run", 60))
    rows = store.unscored_candidates(limit=cap)
    if not rows:
        console.print("[green]Every stored candidate already has an LLM verdict.[/green]")
        store.close()
        return

    jobs = [row_to_job(r) for r in rows]
    console.print(f"[bold]Resuming[/bold]: {len(jobs)} candidates still unscored")
    with console.status("Matching...") as status:
        def progress(done, total):
            status.update(f"Matching... {done}/{total}")
        errors, counts = score_candidates(
            jobs, profile, llm_cfg, store=store, on_progress=progress
        )
    console.print(
        f"  LLM-scored {counts['llm']} · cached {counts['cached']} "
        f"· rules fallback {counts['fallback']}"
    )
    _write_digest(store, errors, set(), counts,
                  max_age_days=config.get("digest", {}).get("max_age_days"),
                  only_new=config.get("digest", {}).get("only_new", True))
    store.close()


def cmd_digest(args) -> None:
    """Rebuild today's digest from jobs.db.

    By default this shows only jobs never put in an earlier day's digest.
    --include-seen rebuilds the full list, ignoring what you have already been
    shown; useful if you deleted a digest file and want it back.
    """
    config = load_yaml("config.yaml")
    store = Store(ROOT / "jobs.db")
    _write_digest(store, [], set(), {},
                  max_age_days=config.get("digest", {}).get("max_age_days"),
                  only_new=not args.include_seen)
    store.close()


# ---------------------------------------------------------------------------
# inspection
# ---------------------------------------------------------------------------

def cmd_stats(_args) -> None:
    store = Store(ROOT / "jobs.db")
    s = store.stats()
    console.print(
        f"[bold]{s['total_seen']}[/bold] jobs retained · "
        f"{s['candidates']} candidates · {s['scored']} scored · "
        f"{s['reported']} reported\n"
    )

    table = Table(title="Match categories", show_header=True, header_style="bold")
    table.add_column("Category")
    table.add_column("Meaning")
    table.add_column("Jobs", justify="right")
    meaning = {
        "A": "strong match", "B": "very good match", "C": "good / adjacent",
        "D": "low match", "E": "not relevant", "-": "not yet scored",
    }
    for letter in ["A", "B", "C", "D", "E", "-"]:
        n = s["by_category"].get(letter, 0)
        if n:
            table.add_row(letter, meaning.get(letter, ""), str(n))
    console.print(table)

    fam = Table(title="Role families", show_header=True, header_style="bold")
    fam.add_column("Family")
    fam.add_column("Jobs", justify="right")
    for name, n in list(s["by_family"].items())[:14]:
        fam.add_row(name, str(n))
    console.print(fam)

    src = Table(title="Sources", show_header=True, header_style="bold")
    src.add_column("Source")
    src.add_column("Jobs", justify="right")
    for name, n in list(s["by_source"].items())[:14]:
        src.add_row(name, str(n))
    console.print(src)
    store.close()


def cmd_top(args) -> None:
    store = Store(ROOT / "jobs.db")
    cats = ("A", "B", "C", "D", "E") if args.all else ("A", "B", "C")
    rows = store.top(limit=args.limit, categories=cats)
    table = Table(show_header=True, header_style="bold")
    table.add_column("Score", justify="right")
    table.add_column("Cat")
    table.add_column("Priority")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Role family")
    for r in rows:
        table.add_row(
            str(r["score"]), r["match_category"] or "-", r["priority"] or "-",
            (r["title"] or "")[:48], (r["company"] or "")[:20],
            (r["role_label"] or "")[:28],
        )
    console.print(table)
    store.close()


def cmd_apply_kit(args) -> None:
    """Build a tailored resume + application note per top-matching job.

    Deliberately stops at 'ready to submit'. Submitting on your behalf is not
    implemented — see the note printed at the end.
    """
    sys.path.insert(0, str(ROOT / "resume"))
    import tailor

    store = Store(ROOT / "jobs.db")
    cats = tuple(args.categories) if args.categories else ("A", "B", "C")
    rows = store.top(limit=args.top, categories=cats)
    store.close()

    if not rows:
        console.print("[yellow]No matching jobs yet. Run `python run.py fetch`.[/yellow]")
        return

    # A kit already built on an earlier day is still valid, and rebuilding it
    # costs an LLM call to produce the same folder again. Skip anything already
    # on disk unless --all is passed.
    apps_root = ROOT / "applications"
    if not args.all:
        built = {
            d.name
            for day in apps_root.glob("*")
            if day.is_dir()
            for d in day.glob("*")
            if d.is_dir()
        }
        fresh = [r for r in rows if tailor.kit_slug(r["company"], r["title"]) not in built]
        skipped = len(rows) - len(fresh)
        rows = fresh
        if skipped:
            console.print(
                f"[dim]{skipped} job(s) already have a kit from an earlier day - "
                f"skipping. Use --all to rebuild them.[/dim]"
            )
        if not rows:
            console.print("[yellow]No new jobs to build kits for.[/yellow]")
            return

    out_dir = apps_root / f"{datetime.now():%Y-%m-%d}"
    console.print(f"[bold]Building {len(rows)} application kits[/bold] -> {out_dir}\n")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Score", justify="right")
    table.add_column("Job")
    table.add_column("Leads with")
    table.add_column("Gaps")
    for row in rows:
        result = tailor.build_for_job(row, out_dir)
        table.add_row(
            str(row["score"]),
            f"{(row['title'] or '')[:38]} — {(row['company'] or '')[:16]}",
            result["lead_skill"][:26],
            ", ".join(result["gaps"][:3]) or "none",
        )
    console.print(table)
    console.print(
        f"\nEach folder has a tailored [bold]resume (.docx + .md)[/bold] and an "
        f"[bold]APPLICATION-NOTE.md[/bold] with the apply URL and expected gaps."
    )
    console.print(
        "[yellow]Submitting these is manual on purpose.[/yellow] Automated "
        "submission breaches the terms of every major job portal and ATS, and "
        "gets accounts banned — which costs you the pipeline you just built."
    )


def cmd_check_setup(_args) -> None:
    """Tell me exactly what is connected and what is still missing."""
    import os
    config = load_yaml("config.yaml")
    sources = config.get("sources") or {}
    t = Table(show_header=True, header_style="bold")
    t.add_column("Source"); t.add_column("Status"); t.add_column("What to do")

    def row(name, ok, detail):
        t.add_row(name,
                  "[green]connected[/green]" if ok else "[yellow]not connected[/yellow]",
                  detail)

    boards = load_yaml("companies.yaml")
    n_boards = sum(len(v or []) for v in boards.values())
    row("Company ATS boards", n_boards > 0, f"{n_boards} boards configured")
    for key in ("remoteok", "arbeitnow", "remotive", "jobicy", "himalayas",
                "weworkremotely"):
        row(f"Feed: {key}", bool(sources.get(key)), "public, no key needed")

    # --- Adzuna ---
    adz = sources.get("adzuna") or {}
    has_keys = bool(adz.get("app_id")) and bool(adz.get("app_key"))
    if adz.get("enabled") and has_keys:
        row("Adzuna (India + 6 more)", True,
            f"{len(adz.get('search_terms') or [])} role terms x "
            f"{len(adz.get('countries') or [])} countries")
    elif has_keys:
        row("Adzuna (India + 6 more)", False,
            "keys present — set sources.adzuna.enabled: true")
    else:
        row("Adzuna (India + 6 more)", False,
            "get a free key at developer.adzuna.com, paste app_id + app_key")

    # --- Mailbox ---
    mail = sources.get("mailbox") or {}
    pw_var = mail.get("password_env", "JOBAGENT_MAIL_PASSWORD")
    has_pw = bool(os.environ.get(pw_var))
    has_user = bool(mail.get("user"))
    if mail.get("enabled") and has_pw and has_user:
        row("Job-alert email (Naukri/LinkedIn)", True,
            f"{mail.get('user')} · folder {mail.get('folder')} · read-only")
    else:
        missing = []
        if not has_user:
            missing.append("set sources.mailbox.user")
        if not has_pw:
            missing.append(f"set the {pw_var} env var (app password)")
        if not mail.get("enabled"):
            missing.append("set sources.mailbox.enabled: true")
        row("Job-alert email (Naukri/LinkedIn)", False, "; ".join(missing))

    console.print(t)
    console.print("\nFull walkthrough: [bold]SETUP.md[/bold]")
    console.print("Verify the mailbox once configured: [bold]python run.py mailbox-test[/bold]")


def cmd_rescore(args) -> None:
    """Re-classify and re-score EVERY stored job under the current rules.

    A fetch only touches the jobs the sources returned today, so a change to
    profile.yaml or roles.py leaves older rows carrying verdicts from the old
    policy. This walks the whole database so a rule change actually applies
    everywhere. Deterministic and free — no LLM calls.
    """
    config = load_yaml("config.yaml")
    profile = load_profile(ROOT / config.get("profile_file", "profile.yaml"))
    store = Store(ROOT / "jobs.db",
                  store_raw=config.get("retention", {}).get("store_raw", False))

    rows = store.conn.execute("SELECT * FROM seen").fetchall()
    console.print(f"[bold]Re-scoring {len(rows)} stored jobs[/bold] under current rules\n")

    changed = demoted = 0
    with console.status("Re-scoring...") as status:
        for i, row in enumerate(rows, 1):
            job = row_to_job(dict(row))
            before_score, before_cat = job.score, job.match_category
            job.apply_classification(classify(job, profile))
            job.scored_by = "rules"          # a rules pass, not an LLM verdict
            rule_match(job, profile)          # applies the scope cap too
            if job.score != before_score or job.match_category != before_cat:
                changed += 1
                if job.match_category in ("D", "E") and before_cat in ("A", "B", "C"):
                    demoted += 1
                store.record_match(job)
            if i % 500 == 0:
                status.update(f"Re-scoring... {i}/{len(rows)}")

    console.print(f"  {changed} jobs changed verdict")
    console.print(f"  [bold]{demoted}[/bold] dropped out of A/B/C — out of scope "
                  f"under the current location / QA-role rules")
    _write_digest(store, [], set(), {},
                  max_age_days=config.get("digest", {}).get("max_age_days"),
                  only_new=config.get("digest", {}).get("only_new", True))
    store.close()


def cmd_mailbox_test(args) -> None:
    """Check the job-alert reader before trusting it with your inbox."""
    from tests.mailbox_cases import run_cases

    console.print("[bold]Parser self-test[/bold] (built-in sample alert emails)\n")
    results = run_cases()
    t = Table(show_header=True, header_style="bold")
    t.add_column("Alert type"); t.add_column("Found", justify="right")
    t.add_column("Expected", justify="right"); t.add_column("URLs clean")
    t.add_column("OK")
    for r in results:
        t.add_row(r["case"], str(r["got"]), str(r["expected"]),
                  "yes" if r["urls_clean"] else "no",
                  "[green]PASS[/green]" if r["ok"] else "[red]FAIL[/red]")
    console.print(t)
    passed = sum(r["ok"] for r in results)
    console.print(f"[bold]{passed}/{len(results)}[/bold] parser cases passed\n")
    if args.offline:
        return

    config = load_yaml("config.yaml")
    mail = (config.get("sources") or {}).get("mailbox") or {}
    if not mail.get("enabled"):
        console.print(
            "[yellow]sources.mailbox.enabled is false.[/yellow] Set it to true, "
            "fill in `user`, and export the app password to connect for real."
        )
        return

    from jobagent.sources.mailbox import fetch_mailbox
    console.print(f"[bold]Connecting[/bold] to {mail.get('host')} as {mail.get('user')} "
                  f"(folder {mail.get('folder')}, read-only)...")
    jobs, errors = fetch_mailbox(mail)
    for e in errors:
        console.print(f"  [red]{e}[/red]")
    console.print(f"\n[bold]{len(jobs)}[/bold] postings found in your alert emails")
    by_portal: dict[str, int] = {}
    for j in jobs:
        by_portal[j.source] = by_portal.get(j.source, 0) + 1
    for portal, n in sorted(by_portal.items(), key=lambda kv: -kv[1]):
        console.print(f"  {n:4}  {portal}")
    for j in jobs[:15]:
        console.print(f"    [dim]{j.source:16}[/dim] {j.title[:52]} — {j.company[:22]}")


def cmd_classify_test(_args) -> None:
    """Role-classification self-test over representative titles."""
    from tests.role_cases import (CASES, GEO_CASES, run_cases, run_geo_cases,
                                  run_remote_cases, run_qa_only_cases,
                                  run_company_cases, run_singapore_cases)

    config = load_yaml("config.yaml")
    profile = load_profile(ROOT / config.get("profile_file", "profile.yaml"))
    results = (run_cases(profile, CASES) + run_geo_cases(profile, GEO_CASES)
               + run_remote_cases(profile) + run_qa_only_cases(profile)
               + run_company_cases(profile) + run_singapore_cases(profile))

    table = Table(show_header=True, header_style="bold")
    table.add_column("Title")
    table.add_column("Expected")
    table.add_column("Got")
    table.add_column("Base", justify="right")
    table.add_column("Cand")
    table.add_column("OK")
    passed = 0
    for r in results:
        passed += r["ok"]
        table.add_row(
            r["title"][:42], r["expected"], r["got"], str(r["base_score"]),
            "yes" if r["candidate"] else "no",
            "[green]PASS[/green]" if r["ok"] else "[red]FAIL[/red]",
        )
    console.print(table)
    console.print(f"\n[bold]{passed}/{len(results)}[/bold] classification cases passed")
    if passed != len(results):
        sys.exit(1)


# ---------------------------------------------------------------------------
# unchanged utilities
# ---------------------------------------------------------------------------

def cmd_verify(_args) -> None:
    boards = load_yaml("companies.yaml")
    console.print("[bold]Checking ATS boards...[/bold]\n")
    results = verify_boards(boards)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Vendor")
    table.add_column("Slug")
    table.add_column("Jobs", justify="right")
    table.add_column("Status")

    live = dead = 0
    for vendor, slug, count, err in sorted(results):
        if err:
            dead += 1
            table.add_row(vendor, slug, "-", f"[red]{err}[/red]")
        elif count == 0:
            table.add_row(vendor, slug, "0", "[yellow]live but empty[/yellow]")
            live += 1
        else:
            live += 1
            table.add_row(vendor, slug, str(count), "[green]ok[/green]")

    console.print(table)
    console.print(f"\n[green]{live} live[/green] · [red]{dead} broken[/red]")
    if dead:
        console.print("Remove or fix the broken slugs in companies.yaml.")


def cmd_tailor(args) -> None:
    config = load_yaml("config.yaml")
    profile = load_profile(ROOT / config.get("profile_file", "profile.yaml"))
    job = Job(
        source="manual",
        company=args.company or "",
        title=args.title or "",
        url=args.url,
        description=args.description or "",
    )
    console.print("[bold]Generating pitch...[/bold]\n")
    console.print(tailor(job, profile, config.get("llm", {})))


def cmd_models(args) -> None:
    """Browse OpenRouter's catalogue. Public endpoint — no API key needed."""
    rows = list_openrouter_models(search=args.search, free_only=args.free)
    if args.structured:
        rows = [r for r in rows if r["structured"]]

    table = Table(show_header=True, header_style="bold")
    table.add_column("Model id")
    table.add_column("$/M in", justify="right")
    table.add_column("$/M out", justify="right")
    table.add_column("Context", justify="right")
    table.add_column("JSON schema", justify="center")

    for row in rows[: args.limit]:
        def money(v):
            if v != v:          # NaN — OpenRouter's variable-pricing sentinel
                return "varies"
            return "free" if v == 0 else f"{v:.3f}"

        price_in, price_out = money(row["in"]), money(row["out"])
        table.add_row(
            row["id"], price_in, price_out,
            f"{row['ctx']:,}" if row["ctx"] else "-",
            "[green]yes[/green]" if row["structured"] else "[dim]no[/dim]",
        )

    console.print(table)
    console.print(
        f"\n{len(rows)} matches. Copy an id into [bold]config.yaml -> llm.model[/bold]."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal job discovery + matching agent")
    sub = parser.add_subparsers(dest="command")

    if len(sys.argv) == 1:
        sys.argv.append("fetch")

    fetch = sub.add_parser("fetch", help="fetch, store, classify, match, digest")
    fetch.add_argument("--no-llm", action="store_true",
                       help="skip LLM matching; deterministic scores only")
    fetch.add_argument("--limit", type=int, help="cap candidates LLM-scored this run")
    fetch.set_defaults(func=cmd_fetch)

    score = sub.add_parser("score", help="resume LLM matching of stored candidates")
    score.add_argument("--limit", type=int)
    score.set_defaults(func=cmd_score)

    dg = sub.add_parser("digest", help="rebuild the digest from jobs.db")
    dg.add_argument("--include-seen", action="store_true",
                    help="also show jobs from earlier days' digests")
    dg.set_defaults(func=cmd_digest)
    sub.add_parser("stats", help="database summary").set_defaults(func=cmd_stats)

    top = sub.add_parser("top", help="best matches in the database")
    top.add_argument("--limit", type=int, default=25)
    top.add_argument("--all", action="store_true", help="include D and E matches")
    top.set_defaults(func=cmd_top)

    kit = sub.add_parser("apply-kit", help="tailored resume + note per top job")
    kit.add_argument("--top", type=int, default=5)
    kit.add_argument("--categories", nargs="*", help="default: A B C")
    kit.add_argument("--all", action="store_true",
                     help="rebuild kits that already exist from an earlier day")
    kit.set_defaults(func=cmd_apply_kit)

    mt = sub.add_parser("mailbox-test", help="check the job-alert email reader")
    mt.add_argument("--offline", action="store_true",
                    help="parser self-test only; do not connect to the mailbox")
    mt.set_defaults(func=cmd_mailbox_test)

    sub.add_parser("check-setup", help="what is connected, what is missing"
                   ).set_defaults(func=cmd_check_setup)

    sub.add_parser("rescore", help="re-apply current rules to every stored job"
                   ).set_defaults(func=cmd_rescore)

    sub.add_parser("classify-test", help="role-classification self-test").set_defaults(
        func=cmd_classify_test)
    sub.add_parser("verify", help="check ATS slugs in companies.yaml").set_defaults(
        func=cmd_verify)

    t = sub.add_parser("tailor", help="write a pitch for one job")
    t.add_argument("url")
    t.add_argument("--company")
    t.add_argument("--title")
    t.add_argument("--description", help="paste the job description")
    t.set_defaults(func=cmd_tailor)

    m = sub.add_parser("models", help="browse OpenRouter models (no key needed)")
    m.add_argument("--search", help="substring filter on the model id")
    m.add_argument("--free", action="store_true", help="only $0 models")
    m.add_argument("--structured", action="store_true",
                   help="only models with JSON-schema support")
    m.add_argument("--limit", type=int, default=30)
    m.set_defaults(func=cmd_models)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
