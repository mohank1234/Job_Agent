from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")
_NONWORD = re.compile(r"[^a-z0-9]+")

REMOTE_HINTS = ("remote", "work from home", "wfh", "anywhere", "distributed")
HYBRID_HINTS = ("hybrid", "flexible location", "partially remote")

ATS_SOURCES = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"}

# Headings that separate the parts of a posting we want to keep addressable.
_SECTION_PATTERNS = {
    "responsibilities": re.compile(
        r"(what you.{0,20}(do|ll be doing)|responsibilities|the role|your role|"
        r"role overview|day to day|in this role)", re.I),
    "requirements": re.compile(
        r"(requirements|qualifications|what you.{0,20}bring|what we.{0,20}looking for|"
        r"you have|must have|skills|experience required|about you)", re.I),
}


def clean_html(text: str | None) -> str:
    if not text:
        return ""
    return _WS.sub(" ", _TAGS.sub(" ", text)).strip()


def normalize(text: str) -> str:
    return _NONWORD.sub("-", (text or "").lower()).strip("-")


def infer_workplace(*parts: str) -> str:
    blob = " ".join(p.lower() for p in parts if p)
    if any(h in blob for h in HYBRID_HINTS):
        return "hybrid"
    if any(h in blob for h in REMOTE_HINTS):
        return "remote"
    if blob.strip():
        return "onsite"
    return "unknown"


@dataclass
class Job:
    source: str
    company: str
    title: str
    url: str
    location: str = ""
    workplace: str = "unknown"
    description: str = ""
    salary: str = ""
    posted_at: datetime | None = None
    raw: dict = field(default_factory=dict)

    # --- stage 1: role classification (jobagent/roles.py, free & offline) ---
    role_family: str = ""
    role_label: str = ""
    role_tier: int = 4
    seniority: str = ""
    base_score: int = 0
    candidate: bool = False
    evidence: dict[str, list[str]] = field(default_factory=dict)
    gap_terms: list[str] = field(default_factory=list)
    years_required: float | None = None
    classification_notes: list[str] = field(default_factory=list)

    # --- stage 2: profile match (rules fallback, or LLM via OmniRoute) ------
    score: int = 0                       # 0-100 match score
    match_category: str = ""             # A / B / C / D / E
    match_category_label: str = ""
    priority: str = ""                   # HIGH / MEDIUM / LOW
    why_matches: list[str] = field(default_factory=list)
    matching_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    experience_gap: str = ""
    recommendation: str = ""
    scored_by: str = ""                  # "llm" | "rules" | "cache"

    # ------------------------------------------------------------- identity
    @property
    def fingerprint(self) -> str:
        """Stable ID across sources — the same job on RemoteOK and the
        company's own Greenhouse board collapses to one entry."""
        key = f"{normalize(self.company)}|{normalize(self.title)}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def content_hash(self) -> str:
        """Changes when the posting — or how we classify it — materially
        changes, so an unchanged job reuses its cached score instead of costing
        another LLM call.

        The classification is part of the hash because it is part of what the
        scorer sees (llm_blob names the role family, tier and seniority). A
        reclassification must therefore invalidate the cached verdict.
        """
        body = (
            f"{self.title}|{self.location}|{self.workplace}|"
            f"{self.role_family}|{self.role_tier}|{self.seniority}|"
            f"{self.description[:4000]}"
        )
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    @property
    def source_type(self) -> str:
        return "ats" if self.source in ATS_SOURCES else "aggregator"

    @property
    def freshness(self) -> tuple[int, str]:
        """Bucket for digest ordering. Fresher is higher priority — a job you
        see on day one is worth far more than the same job on day six.

        Returns (rank, label); lower rank = fresher. Unknown dates sort last
        rather than being dropped, because several boards never publish one.
        """
        age = self.age_days
        if age is None:
            return 9, "date unknown"
        if age <= 1:
            return 0, "last 24 hours"
        if age <= 2:
            return 1, "last 2 days"
        if age <= 4:
            return 2, "last 4 days"
        return 3, "this week"

    @property
    def age_days(self) -> float | None:
        if not self.posted_at:
            return None
        now = datetime.now(timezone.utc)
        posted = self.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (now - posted).total_seconds() / 86400

    # ------------------------------------------------------------- sections
    def section(self, name: str, max_chars: int = 900) -> str:
        """Best-effort slice of responsibilities / requirements out of the
        description. Nothing is destroyed — this is a view, not a rewrite."""
        pattern = _SECTION_PATTERNS.get(name)
        if not pattern or not self.description:
            return ""
        match = pattern.search(self.description)
        if not match:
            return ""
        return self.description[match.start(): match.start() + max_chars].strip()

    def haystack(self) -> str:
        return " ".join(
            [self.title, self.company, self.location, self.description]
        ).lower()

    # ------------------------------------------------------------ pipelines
    def apply_classification(self, c) -> None:
        self.role_family = c.family
        self.role_label = c.label
        self.role_tier = c.tier
        self.seniority = c.seniority
        self.base_score = c.base_score
        self.candidate = c.candidate
        self.evidence = c.evidence
        self.gap_terms = c.gap_terms
        self.years_required = c.years_required
        self.classification_notes = c.notes

    def llm_blob(self, max_chars: int = 1800) -> str:
        """What the scoring model sees. Leads with the parts that actually
        decide the match: role, seniority, responsibilities, requirements."""
        parts = [
            f"Title: {self.title}",
            f"Company: {self.company}",
            f"Location: {self.location or 'n/a'} ({self.workplace})",
            f"Detected role family: {self.role_label} (tier {self.role_tier}, "
            f"{self.seniority} level)",
        ]
        if self.years_required:
            parts.append(f"Stated experience requirement: ~{self.years_required:.0f} years")
        if self.salary:
            parts.append(f"Salary: {self.salary}")
        if self.evidence:
            top = ", ".join(
                f"{g}({len(v)})" for g, v in
                sorted(self.evidence.items(), key=lambda kv: -len(kv[1]))[:6]
            )
            parts.append(f"Skill-domain signals found: {top}")

        resp = self.section("responsibilities", 700)
        reqs = self.section("requirements", 700)
        if resp or reqs:
            if resp:
                parts.append(f"Responsibilities: {resp}")
            if reqs:
                parts.append(f"Requirements: {reqs}")
            remaining = max(0, max_chars - sum(len(p) for p in parts))
            if remaining > 200:
                parts.append(f"Description: {self.description[:remaining]}")
        else:
            parts.append(f"Description: {self.description[:max_chars]}")
        return "\n".join(parts)

    def raw_json(self) -> str:
        try:
            return json.dumps(self.raw, default=str)[:20000]
        except (TypeError, ValueError):
            return "{}"
