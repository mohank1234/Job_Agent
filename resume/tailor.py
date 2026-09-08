"""Per-job resume tailoring.

For a given posting, produce a resume aimed at THAT job description:

  - the headline mirrors the posting's role title
  - skill groups are reordered so the ones the JD talks about most come first
  - experience bullets are reordered the same way
  - a short application note names the overlap in plain words

What it deliberately never does is ADD a skill. Reordering what is true is
tailoring; inserting a tool you have not used is how you fail a screening
call. Anything the JD wants and the profile lacks is reported as a gap so you
go in knowing it.
"""

from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path

import build_resume as base


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9+#./-]{1,}", (text or "").lower()))


def _overlap(candidate_text: str, jd_words: set[str]) -> int:
    """How many distinct meaningful words this line shares with the posting."""
    stop = {
        "and", "the", "for", "with", "from", "that", "this", "across", "using",
        "into", "over", "more", "than", "each", "they", "them", "your", "our",
        "are", "was", "were", "has", "have", "had", "will", "can", "all", "any",
        "out", "own", "run", "ran", "set", "use", "used", "work", "works",
    }
    return len({w for w in _terms(candidate_text) if w in jd_words and w not in stop})


def rank_skills(jd_text: str) -> list[tuple[str, str]]:
    jd_words = _terms(jd_text)
    scored = [
        (_overlap(f"{label} {items}", jd_words), i, (label, items))
        for i, (label, items) in enumerate(base.SKILLS)
    ]
    # Highest overlap first; original order breaks ties so output stays stable.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [group for _score, _i, group in scored]


def rank_experience(jd_text: str) -> list[dict]:
    jd_words = _terms(jd_text)
    out = []
    for job in base.EXPERIENCE:
        bullets = sorted(
            enumerate(job["bullets"]),
            key=lambda ib: (-_overlap(ib[1], jd_words), ib[0]),
        )
        out.append({**job, "bullets": [b for _i, b in bullets]})
    return out          # roles stay reverse-chronological; only bullets move


def headline_for(title: str) -> str:
    """Mirror the posting's own words back, which is what ATS title-matching
    scores against — without claiming a title never held."""
    t = (title or "").lower()
    bits = []
    if re.search(r"sdet|engineer in test|automation", t):
        bits.append("Test Automation")
    if re.search(r"\bai\b|llm|genai|generative|agent|rag|eval", t):
        bits.append("AI and LLM Application Testing")
    if re.search(r"api", t):
        bits.append("API Testing")
    if re.search(r"performance|load", t):
        bits.append("Performance Testing")
    if not bits:
        bits = ["Test Automation", "AI and LLM Application Testing"]
    return "QA Engineer | " + " | ".join(dict.fromkeys(bits))


def gaps_against(jd_text: str) -> list[str]:
    """JD-required tools that are absent from the resume. Reported, never added."""
    resume_text = " ".join(
        [base.SUMMARY]
        + [f"{a} {b}" for a, b in base.SKILLS]
        + [b for job in base.EXPERIENCE for b in job["bullets"]]
    ).lower()
    watch = [
        "cypress", "typescript", "accessibility", "appium", "graphql", "aws",
        "azure", "gcp", "kubernetes", "testrail", "zephyr", "xray", "sauce labs",
        "security testing", "kotlin", "swift", "ruby", "golang", "c#", ".net",
        "salesforce", "sap", "snowflake", "spark", "terraform",
    ]
    jd = (jd_text or "").lower()
    return [
        w for w in watch
        if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", jd)
        and not re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", resume_text)
    ]


def kit_slug(company: str, title: str, posting_id: str = "") -> str:
    """Folder name for one job's kit.

    Shared with run.py so it can tell which jobs already have a kit without
    rebuilding them. Keep the two in step by calling this, never re-deriving.
    """
    name = re.sub(r"[^a-z0-9]+", "-", f"{company or ''}-{title or ''}".lower()).strip("-")[:70]
    return name + ("-" + hashlib.sha256(posting_id.encode()).hexdigest()[:12] if posting_id else "")


def kit_version(job: dict) -> str:
    personal = {k: v for k, v in vars(base).items() if k.isupper()}
    code = Path(__file__).read_text(encoding="utf-8") + Path(base.__file__).read_text(encoding="utf-8")
    text = json.dumps({"job": {k: job.get(k) for k in (
        "url", "description", "title", "company", "score", "match_category", "priority", "location", "workplace")},
        "resume": personal, "code": code}, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def select_kits(rows, apps_root: Path, limit: int, rebuild=False):
    if limit < 0:
        raise ValueError("Kit limit must be non-negative")
    existing = set()
    for path in apps_root.glob("*/*/kit.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            existing.add((meta["url"], meta["version"]))
        except (ValueError, KeyError):
            continue  # damaged metadata requires regeneration, not a silent skip
    # Legacy folders lack a version: keep them intact and create a versioned
    # kit once. Their company/title alone cannot prove which posting they use.
    return [r for r in rows if rebuild or (r.get("url"), kit_version(r)) not in existing][:limit]


def build_for_job(job: dict, out_dir: Path) -> dict:
    """Write a tailored .docx + .md + application note for one posting."""
    jd = job.get("description") or ""
    title = job.get("title") or ""
    company = job.get("company") or ""

    skills = rank_skills(jd)
    experience = rank_experience(jd)
    headline = headline_for(title)
    gaps = gaps_against(jd)

    # Swap the module-level data, render, then restore. build_resume is the
    # single source of truth for the content; this only changes the ordering.
    orig = (base.SKILLS, base.EXPERIENCE, base.TAGLINE)
    base.SKILLS, base.EXPERIENCE, base.TAGLINE = skills, experience, headline

    folder = out_dir / kit_slug(company, title, job.get("url") or job.get("fingerprint", ""))
    folder.mkdir(parents=True, exist_ok=True)
    fname = re.sub(r"[^A-Za-z0-9]+", "_", base.NAME).strip("_") + "_Resume"
    try:
        base.build_docx(folder / f"{fname}.docx")
        base.build_md(folder / f"{fname}.md")
    finally:
        base.SKILLS, base.EXPERIENCE, base.TAGLINE = orig

    note = [
        f"# {title} — {company}",
        "",
        f"- **Match score:** {job.get('score')}/100 ({job.get('match_category')})",
        f"- **Priority:** {job.get('priority')}",
        f"- **Location:** {job.get('location') or 'n/a'} ({job.get('workplace')})",
        f"- **Apply at:** {job.get('url')}",
        "",
        "## Resume changes made for this posting",
        "",
        f"- Headline set to: **{headline}**",
        f"- Skills reordered, leading with: **{skills[0][0]}**, then {skills[1][0]}",
        "- Experience bullets reordered so the ones this JD talks about come first",
        "- No skill was added. Everything here is on the base resume.",
        "",
        "## Gaps to expect in the screening call",
        "",
    ]
    note += [f"- {g}" for g in gaps] or ["- None detected from the tool list."]
    (folder / "APPLICATION-NOTE.md").write_text("\n".join(note), encoding="utf-8")
    from jobagent.runtime import atomic_json
    atomic_json(folder / "kit.json", {"url": job.get("url"), "version": kit_version(job)})

    return {"folder": folder, "headline": headline, "gaps": gaps,
            "lead_skill": skills[0][0]}
