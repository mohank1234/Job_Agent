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

All fetched postings are stored. digest.max_age_days limits displayed and
LLM-scored jobs. Deletion occurs only when retention.prune_non_matching is
explicitly enabled; unchanged tombstones then avoid repeated scoring.
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
from jobagent.matcher import (policy_version, rule_match, rule_match_all,
                              score_candidates, tailor)
from jobagent.models import Job
from jobagent.profile import load_profile
from jobagent.report import build_markdown
from jobagent.roles import candidates as pick_candidates
from jobagent.roles import classify, classify_all
from jobagent.sources import fetch_ats, fetch_feeds, verify_boards
from jobagent.store import Store, row_to_job

ROOT = Path(__file__).parent
RUN_DETAILS: dict = {}

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
    counts = {"ats": 0, "aggregator": 0, "sources": []}

    if sources.get("ats", True):
        boards = load_yaml("companies.yaml")
        with console.status("Fetching company ATS boards..."):
            ats_jobs, ats_errors = fetch_ats(boards, outcomes=counts["sources"])
        counts["ats"] = len(ats_jobs)
        console.print(f"  ATS boards       : {len(ats_jobs)} postings"
                       + (f" ({len(ats_errors)} boards failed)" if ats_errors else ""))
        jobs += ats_jobs
        notes += ats_errors
    else:
        counts["sources"].append({"source": "ats", "status": "disabled", "fetched": 0})

    with console.status("Fetching aggregator feeds..."):
        feed_jobs, feed_errors = fetch_feeds(sources, outcomes=counts["sources"])
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
    pv = policy_version(profile, llm_cfg)

    # Drop anything already judged irrelevant and unchanged since. Pruned rows
    # are gone from `seen`, so the score cache cannot help them — without this
    # check every rejected posting would be re-stored and re-scored by the LLM
    # on every run. The tombstone hash includes the classification AND the
    # matching policy, so a job DOES come back for reconsideration if the
    # classifier, the posting, or the profile/prompt/model changes.
    tombstones = store.dismissed_hashes() if retention.get("prune_non_matching", False) else {}
    if tombstones:
        kept = [j for j in jobs if tombstones.get(j.fingerprint) != j.content_hash(pv)]
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
    new_count, known = store.upsert_all(jobs, policy_version=pv)
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
        console.print("[yellow]--no-llm: stored rules scores; unchanged LLM verdicts retained.[/yellow]\n")
    else:
        cap = args.limit if args.limit is not None else int(llm_cfg.get("max_jobs_per_run", 60))
        if cap < 0:
            raise ValueError("--limit must be non-negative")
        pending = [job for job in cands if not store.apply_cached(job, pv)]
        cached_count = len(cands) - len(pending)
        todo = pending[:cap]
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
        # upsert_all already persisted rules for deferred jobs and preserved
        # unchanged LLM verdicts. Do not overwrite those with rules here.

        with console.status("Matching...") as status:
            def progress(done, total):
                status.update(f"Matching... {done}/{total}")
            errors, scored_counts = score_candidates(
                todo, profile, llm_cfg, store=store, on_progress=progress
            )
        notes += errors
        scored_counts["cached"] += cached_count
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
        "window_days": digest_window,
        **scored_counts,
    }, max_age_days=digest_window,
       only_new=config.get("digest", {}).get("only_new", True))
    store.close()

    RUN_DETAILS.update(sources=counts.get("sources", []), notes=notes, scoring=scored_counts)
    return 2 if notes else 0


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
    stats["window_days"] = max_age_days
    rows = store.top(limit=60, categories=("A", "B", "C"), only_new=only_new,
                     max_age_days=max_age_days)
    jobs = [row_to_job(r) for r in rows]

    # Store.top applies the age window before taking the display quota.

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

    from jobagent.selection import pending_jobs
    cap = args.limit if args.limit is not None else int(llm_cfg.get("max_jobs_per_run", 60))
    if cap < 0:
        raise ValueError("--limit must be non-negative")
    jobs = pending_jobs(store.conn.execute("SELECT * FROM seen").fetchall(), profile, llm_cfg,
                        config.get("digest", {}).get("max_age_days"), store=store)[:cap]
    if not jobs:
        console.print("No eligible fresh candidates need scoring within this run's limit.")
        store.close()
        return

    pv = policy_version(profile, llm_cfg)
    for job in jobs:
        store.record_evaluation(job, pv)
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

    RUN_DETAILS.update(scoring=counts, notes=errors)
    return 2 if errors else 0


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
                  only_new=config.get("digest", {}).get("only_new", True) and not args.include_seen)
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

    config = load_yaml("config.yaml")
    store = Store(ROOT / "jobs.db")
    cats = tuple(args.categories) if args.categories else ("A", "B", "C")
    rows = store.top(limit=-1, categories=cats,
                     max_age_days=config.get("digest", {}).get("max_age_days"))
    store.close()
    apps_root = ROOT / "applications"
    rows = tailor.select_kits(rows, apps_root, args.top, rebuild=args.all)
    if not rows:
        console.print("No fresh matching jobs need a kit within this run's limit.")
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
        "Kits are prepared locally. No application was submitted. "
        "Review the live opening and any unanswered application questions."
    )


def cmd_check_setup(_args) -> None:
    """Tell me exactly what is connected and what is still missing.

    Every row below is backed by a real, minimal live request — no row is
    ever marked "connected" purely because config/env-var fields are
    present. Before this fix (repo audit 2026-09-07 finding #10), a revoked
    mailbox app password still showed "connected" here because the function
    never actually logged in; the separate `mailbox-test` command was the
    only thing that ever really checked. That gap is closed: this command
    now performs the same class of check `mailbox-test`/`verify` do, just
    scoped small enough to stay fast.
    """
    import httpx
    import os

    from jobagent.sources.mailbox import test_connection as _mailbox_test_connection

    config = load_yaml("config.yaml")
    sources = config.get("sources") or {}
    t = Table(show_header=True, header_style="bold")
    t.add_column("Source"); t.add_column("Status"); t.add_column("What to do")

    def row(name, ok, detail):
        t.add_row(name,
                  "[green]connected[/green]" if ok else "[yellow]not connected[/yellow]",
                  detail)

    # --- ATS boards: real live requests against a small sample, not just a
    # count of configured slugs. Sampling (not all ~175 boards) keeps this
    # command fast enough to run before every session, the way `verify` (the
    # full check) is meant to run only after editing companies.yaml.
    boards = load_yaml("companies.yaml")
    n_boards = sum(len(v or []) for v in boards.values())
    sample = {vendor: (slugs or [])[:2] for vendor, slugs in boards.items()}
    sample = {v: s for v, s in sample.items() if s}
    if sample:
        results = verify_boards(sample)
        live = sum(1 for _v, _s, count, err in results if err is None)
        row("Company ATS boards", live > 0,
            f"{n_boards} configured · sample check {live}/{len(results)} boards live")
    else:
        row("Company ATS boards", False, "no boards configured in companies.yaml")

    # --- Aggregator feeds: one real HTTP GET per public feed, not a config
    # presence check. A feed that changed its API or is down now shows up
    # here instead of only failing silently mid-fetch.
    FEED_HEALTH_URLS = {
        "remoteok": "https://remoteok.com/api",
        "arbeitnow": "https://www.arbeitnow.com/api/job-board-api",
        "remotive": "https://remotive.com/api/remote-jobs?limit=1",
        "jobicy": "https://jobicy.com/api/v2/remote-jobs?count=1",
        "himalayas": "https://himalayas.app/jobs/api",
        "weworkremotely": "https://weworkremotely.com/remote-jobs.rss",
    }
    with httpx.Client(timeout=8.0, follow_redirects=True) as client:
        for key, url in FEED_HEALTH_URLS.items():
            if not sources.get(key):
                row(f"Feed: {key}", False, "disabled in config.yaml")
                continue
            try:
                resp = client.get(url, headers={"User-Agent": "job-agent/1.0"})
                ok = resp.status_code < 400
                row(f"Feed: {key}", ok,
                    "reachable" if ok else f"HTTP {resp.status_code}")
            except httpx.HTTPError as exc:
                row(f"Feed: {key}", False, f"{type(exc).__name__}: {exc}")

    # --- Adzuna: a real minimal API call (1 result) when keys are present,
    # not just a check that the fields are non-empty — a typo'd or revoked
    # key used to show the same "not connected, add keys" message as never
    # having configured it at all.
    adz = sources.get("adzuna") or {}
    app_id, app_key = adz.get("app_id"), adz.get("app_key")
    if not (app_id and app_key):
        row("Adzuna (India + 6 more)", False,
            "get a free key at developer.adzuna.com, paste app_id + app_key")
    else:
        try:
            resp = httpx.get(
                "https://api.adzuna.com/v1/api/jobs/in/search/1",
                params={"app_id": app_id, "app_key": app_key, "results_per_page": 1},
                timeout=8.0,
            )
            if resp.status_code == 200:
                row("Adzuna (India + 6 more)",
                    bool(adz.get("enabled")),
                    f"key verified live" + ("" if adz.get("enabled")
                                             else " — set sources.adzuna.enabled: true"))
            elif resp.status_code == 401:
                row("Adzuna (India + 6 more)", False,
                    "app_id/app_key rejected (HTTP 401) — check developer.adzuna.com")
            else:
                row("Adzuna (India + 6 more)", False, f"HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            row("Adzuna (India + 6 more)", False, f"{type(exc).__name__}: {exc}")

    # --- Mailbox: a real IMAP login (see test_connection docstring) rather
    # than a config-presence check.
    mail = sources.get("mailbox") or {}
    if not mail.get("enabled"):
        pw_var = mail.get("password_env", "JOBAGENT_MAIL_PASSWORD")
        missing = []
        if not mail.get("user"):
            missing.append("set sources.mailbox.user")
        if not os.environ.get(pw_var):
            missing.append(f"set the {pw_var} env var (app password)")
        missing.append("set sources.mailbox.enabled: true")
        row("Job-alert email (Naukri/LinkedIn)", False, "; ".join(missing))
    else:
        ok, detail = _mailbox_test_connection(mail)
        row("Job-alert email (Naukri/LinkedIn)", ok, detail)

    # --- Enrichment adapters: one real minimal call each, not a
    # config/env-var presence check — the same standard as everything above.
    enrichment = config.get("enrichment") or {}
    exa_cfg = enrichment.get("exa") or {}
    if exa_cfg.get("enabled"):
        from jobagent.enrich import ExaError, exa_search
        try:
            results = exa_search("test", num_results=1)
            row("Exa (company/job discovery)", True, f"key verified live ({len(results)} result)")
        except ExaError as exc:
            row("Exa (company/job discovery)", False, str(exc))
    else:
        row("Exa (company/job discovery)", False, "enrichment.exa.enabled is false")

    firecrawl_cfg = enrichment.get("firecrawl") or {}
    if firecrawl_cfg.get("enabled"):
        from jobagent.enrich import FirecrawlError, firecrawl_scrape
        try:
            page = firecrawl_scrape("https://example.com")
            row("Firecrawl (page extraction)", True,
                f"key verified live ({len(page['markdown'])} chars from example.com)")
        except FirecrawlError as exc:
            row("Firecrawl (page extraction)", False, str(exc))
    else:
        row("Firecrawl (page extraction)", False, "enrichment.firecrawl.enabled is false")

    browserbase_cfg = enrichment.get("browserbase") or {}
    if browserbase_cfg.get("enabled"):
        key_env = browserbase_cfg.get("api_key_env", "BROWSERBASE_API_KEY")
        row("Browserbase (hosted browser)", False,
            f"{key_env} {'configured' if os.environ.get(key_env) else 'not set'}; session/account login not verified by this check")
    else:
        row("Browserbase (hosted browser)", False, "disabled — no current task needs it")

    # --- Gmail: confirm OAuth is set up (credentials.json present) without
    # forcing a browser consent flow just to run check-setup.
    gmail_cfg = (config.get("outreach") or {}).get("gmail") or {}
    if gmail_cfg.get("enabled"):
        from jobagent.outreach.gmail import CREDENTIALS_PATH, TOKEN_PATH
        if TOKEN_PATH.exists():
            row("Gmail (draft creation)", False, "token file present; consent, scopes and account not live-verified")
        elif CREDENTIALS_PATH.exists():
            row("Gmail (draft creation)", False,
                f"credentials.json present but not yet authorized — run once to complete OAuth consent")
        else:
            row("Gmail (draft creation)", False, f"{CREDENTIALS_PATH.name} not found — see SETUP.md")
    else:
        row("Gmail (draft creation)", False, "outreach.gmail.enabled is false")

    console.print(t)
    console.print("\nFull walkthrough: [bold]SETUP.md[/bold]")
    console.print("Verify the mailbox once configured: [bold]python run.py mailbox-test[/bold]")


def cmd_gmail_auth(args) -> None:
    """One-time Gmail OAuth consent. Run this yourself, interactively — it
    opens your real browser and needs you to click Allow. There is no way
    to automate that click, and there shouldn't be: it's you granting this
    tool access to your Gmail account, not a step to script around.
    """
    from jobagent.outreach.gmail import GmailAuthError, authenticate, get_authenticated_sender

    console.print("[bold]Opening your browser for Gmail consent...[/bold]")
    console.print("If nothing opens, check for a popup blocker or a URL printed below to open by hand.\n")
    try:
        from jobagent.outreach.gmail import SCOPES
        from jobagent.outreach.outcomes import READ_SCOPE
        scopes = SCOPES + ([READ_SCOPE] if args.track_replies else [])
        creds = authenticate(interactive=True, required_scopes=scopes)
        sender = get_authenticated_sender(creds)
    except GmailAuthError as exc:
        console.print(f"[red]Gmail auth failed ({exc.kind}):[/red] {exc}")
        return 1
    console.print(f"[green]Authorized.[/green] Drafts will be created as: [bold]{sender}[/bold]")
    console.print("Token saved to token.json — future runs won't re-prompt unless it's deleted or revoked.")


DRAFT_LEDGER_PATH = ROOT / "outreach_draft_ledger.json"
EMAIL_RE = __import__("re").compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _extract_email(email_cell: str) -> str | None:
    """The Sheet's Email column (split out from the old combined 'Contact
    Value' column on 2026-09-08) should already hold nothing but a bare
    address or be blank. Still run it through the regex rather than trusting
    it verbatim - a stray label or trailing note from an off-schema row
    shouldn't produce a garbage recipient."""
    if not email_cell:
        return None
    m = EMAIL_RE.search(email_cell)
    return m.group(0) if m else None


def cmd_draft_outreach(args) -> int:
    """Create reviewed, evidence-backed drafts, checkpointing every recipient."""
    import json
    from functools import partial
    from jobagent.outreach.gmail import authenticate, create_draft, read_sheet_as_csv, get_authenticated_sender
    from jobagent.outreach.service import parse_rows, draft_rows

    config = load_yaml("config.yaml")
    cfg = config.get("outreach", {}).get("gmail", {})
    if not cfg.get("enabled"):
        raise ValueError("outreach.gmail.enabled is false")
    if args.csv:
        csv_text = Path(args.csv).read_text(encoding="utf-8-sig")
    else:
        file_id = args.sheet_id or cfg.get("sheet_id")
        if not file_id and not args.sheet:
            raise ValueError("Set outreach.gmail.sheet_id or pass --csv / --sheet-id")
        csv_text = read_sheet_as_csv(args.sheet, file_id=file_id)
    rows = parse_rows(csv_text)
    # Preflight account before reserving any recipient or attempting creation.
    create = None
    if not args.dry_run:
        expected = cfg.get("expected_sender")
        if not expected:
            raise ValueError("Set outreach.gmail.expected_sender to the account you intend to use")
        creds = authenticate()
        if get_authenticated_sender(creds).casefold() != expected.casefold():
            raise ValueError("Wrong authenticated Gmail account; no drafts created")
        create = partial(create_draft, creds=creds, expected_sender=expected)
    result = draft_rows(rows, DRAFT_LEDGER_PATH, create, dry_run=args.dry_run, limit=args.limit)
    console.print(json.dumps(result, indent=2))
    console.print("Drafts only. No email was sent by this command.")
    if result["uncertain"]:
        console.print("An API outcome is uncertain. Inspect Gmail and the ledger before any retry.")
    invalid = sum(n for reason, n in result["skipped"].items()
                  if reason not in ("already_reserved_or_drafted", "over_limit"))
    return 2 if result["uncertain"] or invalid else 0
def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def cmd_verify_outreach(args):
    import csv
    import io
    import httpx
    from collections import Counter
    from jobagent.outreach.gmail import read_sheet_as_csv
    from jobagent.outreach.verification import verify_row, csv_text, evidence_markdown
    from jobagent.runtime import atomic_json, now_iso

    config = load_yaml("config.yaml")
    profile = load_profile(ROOT / config.get("profile_file", "profile.yaml"))
    if args.csv:
        source = Path(args.csv).read_text(encoding="utf-8-sig")
    else:
        source = read_sheet_as_csv(file_id=args.sheet_id)
    reader = csv.DictReader(io.StringIO(source))
    if not {"Company", "Job Link"}.issubset(reader.fieldnames or []):
        raise ValueError("Input must contain Company and Job Link columns")
    rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        raise ValueError("Malformed CSV row")
    if not 1 <= args.limit <= 100:
        raise ValueError("Verification limit must be between 1 and 100")
    cache, verified = {}, []
    with httpx.Client() as client:
        for row in rows[:args.limit]:
            result = verify_row(row, profile, client, board_cache=cache, use_firecrawl=args.firecrawl)
            verified.append(result)
            console.print(f"{row['Company']}: {result['JD Status']} / {result['Fit Status']}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "verified.csv").write_text(csv_text(verified), encoding="utf-8-sig")
    (out / "evidence.md").write_text(evidence_markdown(verified), encoding="utf-8")
    summary = {"checked_at": now_iso(), "input_rows": len(rows), "checked": len(verified),
               "not_checked": max(0, len(rows) - len(verified)),
               "statuses": dict(Counter(row["JD Status"] for row in verified))}
    atomic_json(out / "verification.json", summary)
    RUN_DETAILS.update(summary)
    console.print(f"Evidence and full JDs: {out.resolve()}")
    return 2 if summary["not_checked"] or any(row["JD Status"] in (
        "fetch_error", "blocked", "description_missing", "unsupported_source", "ambiguous_posting") for row in verified) else 0


def cmd_discover(args):
    from jobagent.enrich import exa_search
    from jobagent.outreach.verification import csv_text, board_ref
    if not 1 <= args.limit <= 10:
        raise ValueError("Discovery limit must be between 1 and 10")
    results = exa_search(args.query, num_results=args.limit,
                         include_domains=["jobs.ashbyhq.com", "jobs.lever.co", "job-boards.greenhouse.io"])
    rows = [{"Company": board_ref(r["url"])[1] if board_ref(r["url"]) else "",
             "Job Link": r["url"], "Discovery Title (unverified)": r["title"],
             "JD Status": "search_result_unverified"} for r in results]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(csv_text(rows), encoding="utf-8-sig")
    console.print(f"{len(rows)} unverified leads saved to {out}; run verify-outreach to fetch actual postings.")


def cmd_outreach_status(args):
    import json
    from jobagent.outreach.outcomes import observe
    from jobagent.runtime import atomic_json
    ledger = json.loads(DRAFT_LEDGER_PATH.read_text(encoding="utf-8"))
    cfg = load_yaml("config.yaml").get("outreach", {}).get("gmail", {})
    if not cfg.get("expected_sender"):
        raise ValueError("Set outreach.gmail.expected_sender")
    result = observe(ledger, cfg["expected_sender"])
    atomic_json(Path(args.out), result)
    console.print(f"Outcome evidence saved to {args.out}. Incoming messages still need human interpretation.")
    return 2 if any(o["truncated"] for o in result["observations"]) else 0


def cmd_drive_auth(args):
    from jobagent.drive_output import authenticate_drive
    cfg = load_yaml("config.yaml").get("drive_output", {})
    authenticate_drive(cfg.get("expected_account"), interactive=True)
    console.print("Drive access verified. Report publishing can now create and update its own files.")


def cmd_publish_report(args):
    from jobagent.drive_output import publish_report
    cfg = load_yaml("config.yaml").get("drive_output", {})
    result = publish_report(Path(args.out), cfg.get("expected_account"), cfg.get("folder_name", "JobAgent Output"), main_files_only=cfg.get('main_files_only', False))
    RUN_DETAILS.update(publication=result)
    console.print(f"Folder: {result['folder_url']}")
    console.print(f"Verified report: {result['sheet_url']}")


def cmd_refresh_report(args):
    import json
    from jobagent.output import build_report, database_leads, read_leads
    from jobagent.startup_output import research_leads
    from jobagent.runtime import process_lock
    config = load_yaml("config.yaml")
    profile = load_profile(ROOT / config.get("profile_file", "profile.yaml"))
    cfg = config.get("drive_output", {})
    if not 1 <= args.limit <= 100:
        raise ValueError("Report limit must be between 1 and 100")
    out = Path(args.out)
    with process_lock(ROOT / "logs" / "refresh-report.lock"):
        leads = research_leads(out)
        leads += read_leads(args.csv) if args.csv else []
        previous = out / "verified.csv"
        if previous.exists():
            leads += [r for r in read_leads(previous) if r.get("JD Status") == "verified_live"
                      and r.get("Fit Status") != "out_of_scope"
                      and "Intermediary" not in r.get("Listing Type", "")
                      and "/jobgether/" not in r.get("Job Link", "")]
        leads += database_leads(ROOT / config.get("database", "jobs.db"), profile, args.limit)
        queries = cfg.get("discovery_queries", []) if args.discover else []
        summary, rows = build_report(profile, leads, out, limit=args.limit,
                                     discovery_queries=queries, progress=console.print)
        RUN_DETAILS.update(report=summary)
        console.print(json.dumps(summary, indent=2))
        console.print(f"Local report: {(out / 'JobAgent Report.xlsx').resolve()}")
        if args.publish:
            cmd_publish_report(args)
        return 2 if summary["status"] == "partial" else 0


def cmd_morning(args):
    import json
    from jobagent.morning import run_daily, daily_work
    from jobagent.drive_output import authenticate_drive, Publisher, STATE_PATH
    from jobagent.runtime import process_lock
    config = load_yaml('config.yaml')
    profile = load_profile(ROOT / config.get('profile_file', 'profile.yaml'))
    cfg = config.get('drive_output', {})
    connection = {}
    def preflight():
        connection['drive'] = authenticate_drive(cfg.get('expected_account'))
    def publish(out):
        with process_lock(STATE_PATH.with_suffix('.lock')):
            return Publisher(connection['drive']).publish(out, cfg.get('folder_name', 'JobAgent Output'), main_files_only=cfg.get('main_files_only', False))
    result = run_daily(config, profile, Path(args.out), initial=args.initial,
                       work=daily_work, publish=publish, preflight=preflight, progress=console.print)
    RUN_DETAILS.update(result)
    console.print(json.dumps(result, indent=2))
    return 2 if result.get('status') == 'window_ended' else 0


def cmd_rescore(args) -> None:
    """Re-classify and re-score EVERY stored job under the current rules.

    A fetch only touches the jobs the sources returned today, so a change to
    profile.yaml or roles.py leaves older rows carrying verdicts from the old
    policy. This walks the whole database so a rule change actually applies
    everywhere. Deterministic and free — no LLM calls.
    """
    config = load_yaml("config.yaml")
    llm_cfg = config.get("llm", {})
    profile = load_profile(ROOT / config.get("profile_file", "profile.yaml"))
    store = Store(ROOT / "jobs.db",
                  store_raw=config.get("retention", {}).get("store_raw", False))
    pv = policy_version(profile, llm_cfg)

    rows = store.conn.execute("SELECT * FROM seen").fetchall()
    console.print(f"[bold]Re-scoring {len(rows)} stored jobs[/bold] under current rules\n")

    changed = demoted = reclassified = 0
    with console.status("Re-scoring...") as status:
        for i, row in enumerate(rows, 1):
            job = row_to_job(dict(row))
            before_score, before_cat = job.score, job.match_category
            before_family, before_candidate = row["role_family"], bool(row["candidate"])
            job.apply_classification(classify(job, profile))
            job.scored_by = "rules"          # a rules pass, not an LLM verdict
            rule_match(job, profile)          # applies the scope cap too

            # Stage-1 fields (role_family/candidate/base_score/...) must be
            # persisted whenever they changed, independent of whether the
            # final score/category also changed — record_match alone never
            # writes them, which is exactly repo audit finding #13: a
            # reclassification's role_family/candidate/base_score stayed
            # permanently stale in the DB even after match_category updated.
            if job.content_hash(pv) != row["content_hash"]:
                reclassified += 1

            if job.score != before_score or job.match_category != before_cat:
                changed += 1
                if job.match_category in ("D", "E") and before_cat in ("A", "B", "C"):
                    demoted += 1
            store.record_evaluation(job, pv)
            if i % 500 == 0:
                status.update(f"Re-scoring... {i}/{len(rows)}")

    console.print(f"  {reclassified} jobs got an updated stage-1 classification persisted")
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
        return 0 if passed == len(results) else 1

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
    return 1 if errors or passed != len(results) else 0


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
    return 1 if dead else 0


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

    auth = sub.add_parser("gmail-auth", help="one-time Gmail OAuth consent (opens your browser)")
    auth.add_argument("--track-replies", action="store_true", help="also request read-only Gmail scope for outcome tracking")
    auth.set_defaults(func=cmd_gmail_auth)
    outcomes = sub.add_parser("outreach-status", help="read sent/thread evidence; requires gmail-auth --track-replies")
    outcomes.add_argument("--out", default="research/outreach-status.json")
    outcomes.set_defaults(func=cmd_outreach_status)

    draft = sub.add_parser("draft-outreach",
                           help="create Gmail drafts from the Outreach Report sheet (never sends)")
    draft.add_argument("--sheet", default=None,
                       help="explicit legacy name lookup; multiple matches fail")
    draft.add_argument("--sheet-id", help="fixed Google Sheet ID")
    draft.add_argument("--csv", help="local reviewed CSV; dry-run with this option is offline")
    draft.add_argument("--limit", type=int, default=10)
    draft.add_argument("--dry-run", action="store_true",
                       help="show what would be drafted without creating anything")
    draft.set_defaults(func=cmd_draft_outreach)

    research = sub.add_parser("verify-outreach", help="verify public ATS postings and exact public contact evidence")
    inputs = research.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--csv")
    inputs.add_argument("--sheet-id")
    research.add_argument("--out", default="research/latest")
    research.add_argument("--limit", type=int, default=30)
    research.add_argument("--firecrawl", action="store_true", help="extract unsupported pages as unverified evidence (uses credits)")
    research.set_defaults(func=cmd_verify_outreach)
    discover = sub.add_parser("discover", help="bounded Exa discovery; results require ATS verification")
    discover.add_argument("query")
    discover.add_argument("--limit", type=int, default=5)
    discover.add_argument("--out", default="research/discovered.csv")
    discover.set_defaults(func=cmd_discover)

    sub.add_parser("drive-auth", help="one-time consent to create/update this app's Drive output files").set_defaults(func=cmd_drive_auth)
    publish = sub.add_parser("publish-report", help="publish and read back an already prepared report bundle")
    publish.add_argument("--out", default="research/report-latest")
    publish.set_defaults(func=cmd_publish_report)
    refresh = sub.add_parser("refresh-report", help="fetch full current JDs and produce a verified vacancy report")
    refresh.add_argument("--csv", help="additional vacancy links to verify")
    refresh.add_argument("--out", default="research/report-latest")
    refresh.add_argument("--limit", type=int, default=50)
    refresh.add_argument("--discover", action="store_true", help="also use configured bounded Exa searches (uses credits)")
    refresh.add_argument("--publish", action="store_true", help="publish output into the permanent Drive folder and report")
    refresh.set_defaults(func=cmd_refresh_report)

    morning = sub.add_parser('morning', help='once-daily 06:00-11:00 IST search, unsent outreach and Drive publication')
    morning.add_argument('--out', default='research/report-latest')
    morning.add_argument('--initial', action='store_true', help='explicitly authorized one-time setup run outside the morning window')
    morning.set_defaults(func=cmd_morning)

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

    from jobagent.runtime import atomic_json, now_iso
    args = parser.parse_args()
    started = now_iso()
    RUN_DETAILS.clear()
    code = 1
    try:
        code = args.func(args) or 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        RUN_DETAILS["error_type"] = type(exc).__name__
        console.print(f"[red]{type(exc).__name__}[/red]: " +
                      (str(exc) if isinstance(exc, (ValueError, FileNotFoundError))
                       or type(exc).__name__ == "GmailAuthError" else "Command failed; no success recorded."))
    finally:
        if args.command in ("fetch", "score", "digest", "apply-kit", "draft-outreach", "verify-outreach", "rescore", "refresh-report", "publish-report", "morning") and not getattr(args, "dry_run", False):
            atomic_json(ROOT / "logs" / f"last-{args.command}.json", {
                "command": args.command, "started_at": started, "finished_at": now_iso(),
                "status": "ok" if code == 0 else "partial" if code == 2 else "failed",
                "exit_code": code, **RUN_DETAILS})
    raise SystemExit(code)


if __name__ == "__main__":
    main()
