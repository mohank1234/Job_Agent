"""Stage 2 — profile matching.

Stage 1 (roles.py) already labelled every stored job for free. This module
turns candidates into a scored, explained, prioritised recommendation.

Two paths, both always available:

  rule_match()   deterministic, offline, no LLM. Runs on EVERYTHING, so every
                 job in the database always has a score and a category.
  score_candidates()  detailed semantic comparison against the full profile,
                 via OmniRoute + a free model. Candidates only, batched,
                 cached, resumable, and degrades to rule_match on failure.

The final score blends the two. That is deliberate: the deterministic score
stops a free model from inflating a generic SWE posting into a top match, and
the LLM score stops the rule engine from rewarding raw keyword count.
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .llm import ProviderError, make_provider
from .models import Job
from .profile import Profile
from .roles import apply_scope_cap, category_for, priority_for

SYSTEM = """You are screening job postings for ONE specific candidate. You are \
an experienced technical recruiter who places QA, SDET, test automation and \
AI-quality/evaluation engineers.

Judge the ROLE first, the technologies second. Technologies (Selenium, \
Playwright, Postman, Java, Python, RAG, LLM, agents, computer vision) are \
signals of how well the candidate fits a role — they are never themselves a \
role. Recognise equivalent titles: "Senior QA Engineer" ~ "Senior Quality \
Assurance Engineer" ~ "Senior Software QA Engineer"; "QA Automation Engineer" \
~ "Test Automation Engineer" ~ "SDET" ~ "Software Engineer in Test"; "AI QA \
Engineer" ~ "AI Test Engineer" ~ "AI Evaluation Engineer" ~ "LLM Evaluation \
Engineer".

Score 0-100 for REALISTIC suitability — could this candidate apply and \
plausibly get shortlisted?

  90-100  excellent, strong match: role, responsibilities and skills all align
  80-89   very good match, minor gaps
  70-79   good match, some real gaps
  60-69   adjacent — worth considering
  below 60 low match

Rules you must follow:

1. EXPERIENCE IS FLEXIBLE, NOT A FILTER. If the posting asks for somewhat more \
years than the candidate has, but responsibilities and skills match strongly, \
keep the score high and note the gap. Only reduce the score significantly when \
the role genuinely demands far more seniority — architecture ownership, people \
management, research-level depth, or a decade-plus of experience.

2. SENIORITY IS FLEXIBLE. One reasonable level above the candidate's level is \
acceptable on a strong technical match.

3. NORMAL QA JOBS MATTER. A plain Senior QA Engineer or QA Automation Engineer \
role can be a better opportunity than an AI role. Do not favour AI roles \
automatically.

4. AI-QUALITY JOBS MATTER TOO. The candidate has real hands-on AI/LLM/RAG/agent\
/chatbot/CV testing and evaluation experience. Score those roles on their merits.

5. SOFTWARE ENGINEER ROLES ARE NOT AUTOMATICALLY REJECTED. Include a SWE role \
when its actual responsibilities overlap with testing, quality engineering, test \
automation, API automation, test infrastructure, AI application testing, or \
LLM/RAG/agent evaluation. Score a SWE role LOW when it is really frontend, \
React/Angular, backend/Node service development, distributed systems, data \
engineering, data science, pure ML engineering, DevOps or cloud engineering \
with no quality/evaluation responsibility.

6. NEVER INVENT EXPERIENCE. Judge only against what the profile actually states. \
If a required skill is absent from the profile, it belongs in missing_skills.

7. Do not inflate a score because many keywords appear. Score realistic \
suitability.

8. WORK LOCATION — absolute, no exceptions. The candidate is in Hyderabad, \
India and will not relocate.
   REMOTE counts ONLY if someone based in India can actually take the job. \
Most postings marked "remote" are remote within ONE country. If the posting \
says "Remote - US", "Canada", "Brazil - Remote", "San Francisco", or requires \
work authorisation elsewhere, it is NOT applicable: score below 40, LOW \
priority, and say so in `gap`.
   ONSITE or HYBRID counts ONLY in Hyderabad. Not Bengaluru, not Pune, not any \
other Indian city, never outside India. Anything else scores below 40.
   A role genuinely open worldwide, or explicitly open to India or APAC, is in \
scope and scores on its merits.

9. COMPENSATION. The candidate will not move below the stated minimum. When a \
posting states pay clearly below that minimum, say so in `gap` and reduce the \
priority — but most postings state nothing, so never assume.

10. EMPLOYER QUALITY. Prefer product companies that pay well in India (Atlassian, Postman, BrowserStack, Databricks, Snowflake, Stripe, Okta, Atlassian, Razorpay, PhonePe, Paytm, Swiggy, Flipkart, Adobe, Salesforce, ServiceNow, NVIDIA, Microsoft, Google, Glean, Rubrik and similar). Score DOWN a posting from a staffing, recruitment or manpower consultancy that is hiring for an undisclosed client: the employer is unknown, the pay band is usually lower and the process is opaque. Say so in `gap` when it applies.

11. ONLY QA ROLES. The role itself must be testing, quality assurance, test \
automation, SDET, or AI/model evaluation. A software developer role is NOT in \
scope even when its description mentions testing heavily — "Software Engineer, \
Generative AI", "Senior Software Engineer, Agents" and "Full Stack Engineer" \
all score below 40.

Fields: `why` = short concrete phrases explaining the match. \
`matching_skills` = candidate skills the posting actually asks for. \
`missing_skills` = required skills the candidate does not have. \
`gap` = one short sentence on the experience/seniority gap, or "none". \
`recommendation` = one short sentence on whether and how to apply."""


class JobMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    score: int
    priority: Literal["HIGH", "MEDIUM", "LOW"]
    why: list[str]
    matching_skills: list[str]
    missing_skills: list[str]
    gap: str
    recommendation: str


class MatchBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matches: list[JobMatch]


# ---------------------------------------------------------------------------
# Deterministic path — always runs, so nothing is ever left unscored
# ---------------------------------------------------------------------------

GROUP_LABELS = {
    "qa": "core QA / testing",
    "automation": "test automation",
    "api": "API testing",
    "ci_cd": "CI/CD",
    "ai_llm": "AI / LLM",
    "evaluation": "evaluation & benchmarking",
    "rag": "RAG",
    "agents": "AI agents",
    "conversational": "chatbot / conversational AI",
    "computer_vision": "computer vision",
    "programming": "programming",
    "ai_assisted_dev": "AI-assisted development (Copilot/Claude)",
}


def rule_match(job: Job, profile: Profile) -> None:
    """Fill score + category + priority + explanation from the classification
    alone. No network, no cost."""
    job.score = job.base_score
    letter, label = category_for(job.score)
    job.match_category, job.match_category_label = letter, label

    why: list[str] = []
    if job.role_label:
        why.append(f"Role family: {job.role_label}")
    for group in list(job.evidence)[:5]:
        hits = job.evidence[group]
        why.append(f"{GROUP_LABELS.get(group, group)} overlap ({len(hits)} signals)")
    job.why_matches = why

    matching: list[str] = []
    for _group, hits in sorted(job.evidence.items(), key=lambda kv: -len(kv[1])):
        matching.extend(hits[:4])
    job.matching_skills = matching[:12]
    job.missing_skills = job.gap_terms[:10]

    gap_notes = [n for n in job.classification_notes if "gap" in n or "level" in n]
    job.experience_gap = "; ".join(gap_notes) if gap_notes else "none detected"
    job.priority = priority_for(job.score, job.role_tier, len(job.missing_skills),
                                freshness_rank=job.freshness[0])
    job.recommendation = (
        f"{job.priority} priority — {label.lower()} on deterministic role/skill "
        f"matching (no LLM verdict yet)."
    )
    if job.scored_by != "llm":
        job.scored_by = "rules"
    apply_scope_cap(job, profile, profile.qa_roles_only)


def rule_match_all(jobs: list[Job], profile: Profile) -> None:
    for job in jobs:
        rule_match(job, profile)


# ---------------------------------------------------------------------------
# LLM path — OmniRoute + a free model
# ---------------------------------------------------------------------------

def _apply_llm(job: Job, entry: JobMatch, llm_weight: float,
               profile: Profile | None = None) -> None:
    """Blend the model's judgement with the deterministic score, then derive
    category and priority from the result."""
    llm_score = max(0, min(100, int(entry.score)))
    blended = round(llm_weight * llm_score + (1 - llm_weight) * job.base_score)
    job.score = int(max(0, min(100, blended)))

    letter, label = category_for(job.score)
    job.match_category, job.match_category_label = letter, label
    job.why_matches = [w.strip() for w in entry.why if w.strip()][:8]
    job.matching_skills = [s.strip() for s in entry.matching_skills if s.strip()][:14]
    job.missing_skills = [s.strip() for s in entry.missing_skills if s.strip()][:12]
    job.experience_gap = entry.gap.strip() or "none"
    job.recommendation = entry.recommendation.strip()

    # The model proposes a priority; the score and role tier confirm it, so a
    # low-scoring posting can never come back as HIGH.
    ceiling = priority_for(job.score, job.role_tier, len(job.missing_skills),
                           freshness_rank=job.freshness[0])
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    job.priority = entry.priority if order[entry.priority] <= order[ceiling] else ceiling
    job.scored_by = "llm"
    # Location and role rules are absolute: they override the model.
    if profile is not None:
        apply_scope_cap(job, profile, profile.qa_roles_only)


def match_batch(provider, jobs: list[Job], profile: Profile, llm_weight: float) -> None:
    listing = "\n\n".join(
        f"--- JOB {i} ---\n{job.llm_blob()}" for i, job in enumerate(jobs)
    )
    prompt = (
        f"{profile.llm_block()}\n"
        f"Evaluate all {len(jobs)} postings below against this profile. "
        f"Return exactly {len(jobs)} entries, using the JOB number as `index`.\n\n"
        f"{listing}"
    )
    batch = provider.structured(SYSTEM, prompt, MatchBatch)
    seen: set[int] = set()
    for entry in batch.matches:
        if 0 <= entry.index < len(jobs) and entry.index not in seen:
            seen.add(entry.index)
            _apply_llm(jobs[entry.index], entry, llm_weight, profile)


def score_candidates(
    jobs: list[Job],
    profile: Profile,
    llm_cfg: dict,
    store=None,
    on_progress=None,
) -> tuple[list[str], dict]:
    """Score candidates through the LLM, batched and resumable.

    Returns (non-fatal errors, counters). Every job passed in leaves this
    function scored — via cache, LLM, or the deterministic fallback.
    """
    size = max(1, int(llm_cfg.get("batch_size", 4)))
    retries = max(0, int(llm_cfg.get("retries", 2)))
    backoff = float(llm_cfg.get("backoff_seconds", 4))
    llm_weight = float(llm_cfg.get("llm_weight", 0.75))
    errors: list[str] = []
    counts = {"cached": 0, "llm": 0, "fallback": 0}

    # Cache pass first — an unchanged posting never costs another call.
    pending: list[Job] = []
    for job in jobs:
        if store is not None and store.apply_cached(job):
            counts["cached"] += 1
        else:
            pending.append(job)

    if not pending:
        if on_progress:
            on_progress(len(jobs), len(jobs))
        return errors, counts

    try:
        provider = make_provider(llm_cfg)
    except ProviderError as exc:
        errors.append(f"LLM unavailable ({exc}) — deterministic scoring only")
        for job in pending:
            rule_match(job, profile)
            counts["fallback"] += 1
            if store is not None:
                store.record_match(job)
        return errors, counts

    done = counts["cached"]
    for start in range(0, len(pending), size):
        chunk = pending[start: start + size]
        ok = False
        for attempt in range(retries + 1):
            try:
                match_batch(provider, chunk, profile, llm_weight)
                ok = True
                break
            except Exception as exc:
                if attempt < retries:
                    time.sleep(backoff * (2 ** attempt))   # exponential backoff
                else:
                    errors.append(
                        f"batch @{start}: {type(exc).__name__}: {str(exc)[:160]}"
                    )

        for job in chunk:
            if ok and job.scored_by == "llm":
                counts["llm"] += 1
            else:
                rule_match(job, profile)      # never leave a candidate unscored
                counts["fallback"] += 1
            # Checkpoint after every batch so an interrupted run resumes here.
            if store is not None:
                store.record_match(job)

        done += len(chunk)
        if on_progress:
            on_progress(done, len(jobs))

    used = getattr(provider, "last_model_used", None)
    if used:
        counts["model"] = used
        # The digest names the provider; take it from the provider actually in
        # use rather than hardcoding one, which is how it went on crediting
        # OmniRoute for days after OmniRoute was removed.
        counts["provider"] = getattr(provider, "name", None)
    return errors, counts


# ---------------------------------------------------------------------------
# Pitch writer
# ---------------------------------------------------------------------------

TAILOR_SYSTEM = """You write short, specific job applications for one candidate.

No fluff, no "I am writing to express my interest", no restating the job ad back \
at them. Lead with the single most relevant concrete thing the candidate has \
done. Name real skills from their background — never invent experience they do \
not have. Write like a competent engineer emailing a hiring manager, not like a \
cover letter template. Maximum 150 words."""


def tailor(job: Job, profile: Profile, llm_cfg: dict) -> str:
    provider = make_provider(llm_cfg)
    return provider.text(
        TAILOR_SYSTEM,
        f"{profile.llm_block()}\nWrite the pitch for this role.\n\n"
        f"{job.llm_blob(2600)}",
    )
