"""Central profile loader.

`profile.yaml` is the single source of truth for the candidate. Nothing about
the candidate belongs in Python — roles.py and matcher.py read everything from
the object this module builds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).parent.parent / "profile.yaml"

# Ordered ladder. Index distance drives the seniority-fit penalty, so the
# matcher never needs a hardcoded "only N years" rule.
SENIORITY_LADDER = [
    "intern",
    "junior",
    "mid",
    "mid_senior",
    "senior",
    "lead",
    "staff",
    "principal",
    "architect",
    "manager",
    "director",
    "head",
    "vp",
    "executive",
]


@dataclass
class Profile:
    raw: dict = field(default_factory=dict)

    # -------------------------------------------------------------- identity
    @property
    def headline(self) -> str:
        return self.raw.get("identity", {}).get("headline", "")

    @property
    def summary(self) -> str:
        return self.raw.get("identity", {}).get("summary", "").strip()

    @property
    def experience(self) -> dict:
        return self.raw.get("identity", {}).get("experience", {}) or {}

    @property
    def years(self) -> float:
        return float(self.experience.get("years", 0) or 0)

    @property
    def stretch_years(self) -> float:
        """How far above `years` a stated requirement may go before it starts
        to hurt. Deliberately generous — a small gap is acceptable."""
        return float(self.experience.get("stretch_years", 3) or 3)

    @property
    def hard_gap_years(self) -> float:
        return float(self.experience.get("hard_gap_years", 6) or 6)

    # ---------------------------------------------------------------- levels
    @property
    def self_level(self) -> str:
        return self.raw.get("seniority", {}).get("self_level", "mid_senior")

    @property
    def self_level_index(self) -> int:
        try:
            return SENIORITY_LADDER.index(self.self_level)
        except ValueError:
            return SENIORITY_LADDER.index("mid_senior")

    @property
    def reject_levels(self) -> set[str]:
        return {s.lower() for s in self.raw.get("seniority", {}).get("reject", [])}

    # ---------------------------------------------------------------- skills
    @property
    def skills(self) -> dict[str, list[str]]:
        return {
            group: [s.lower() for s in terms or []]
            for group, terms in (self.raw.get("skills") or {}).items()
        }

    def skill_group(self, name: str) -> list[str]:
        return self.skills.get(name, [])

    @cached_property
    def all_skills(self) -> list[str]:
        out: list[str] = []
        for terms in self.skills.values():
            out.extend(terms)
        return sorted(set(out))

    @property
    def gaps(self) -> list[str]:
        return [g.lower() for g in self.raw.get("gaps") or []]

    # ----------------------------------------------------------- preferences
    @property
    def preferences(self) -> dict:
        return self.raw.get("preferences") or {}

    @property
    def locations(self) -> list[str]:
        return [l.lower() for l in self.preferences.get("locations") or []]

    @property
    def remote_preference(self) -> str:
        return self.preferences.get("remote_preference", "mild")

    @property
    def onsite_cities(self) -> list[list[str]]:
        """Ranked groups of acceptable onsite/hybrid cities; first = best."""
        groups = self.preferences.get("onsite_cities") or []
        out: list[list[str]] = []
        for group in groups:
            if isinstance(group, str):
                out.append([group.lower()])
            else:
                out.append([c.lower() for c in group])
        return out

    @property
    def qa_roles_only(self) -> bool:
        return bool(self.preferences.get("qa_roles_only", True))

    @property
    def preferred_companies(self) -> list[str]:
        return [c.lower() for c in self.preferences.get("preferred_companies") or []]

    @property
    def staffing_companies(self) -> list[str]:
        return [c.lower() for c in self.preferences.get("staffing_companies") or []]

    @property
    def preferred_company_bonus(self) -> float:
        return float(self.preferences.get("preferred_company_bonus", 12) or 0)

    @property
    def staffing_agency_penalty(self) -> float:
        return float(self.preferences.get("staffing_agency_penalty", 14) or 0)

    @property
    def onsite_abroad(self) -> bool:
        """Whether onsite work outside India is considered at all.

        Accepts the historical booleans and the newer named form
        ("singapore_only"), which reads as True here - which countries are
        actually in scope is decided by onsite_countries.
        """
        value = self.preferences.get("onsite_abroad", True)
        if isinstance(value, str):
            return value.strip().lower() not in ("", "false", "no", "none")
        return bool(value)

    @property
    def onsite_countries(self) -> list[str]:
        """Countries where onsite/hybrid is in scope, beyond onsite_cities."""
        value = self.preferences.get("onsite_abroad", False)
        if isinstance(value, str) and value.strip().lower().endswith("_only"):
            return [value.strip().lower().rsplit("_only", 1)[0]]
        return []

    @property
    def singapore(self) -> dict:
        return self.preferences.get("singapore") or {}

    @property
    def ep_min_monthly_sgd(self) -> float:
        """MOM's Employment Pass salary floor. Below it no employer can sponsor."""
        return float(self.singapore.get("ep_min_monthly_sgd", 5600) or 0)

    @property
    def ep_min_monthly_sgd_finance(self) -> float:
        return float(self.singapore.get("ep_min_monthly_sgd_finance", 6200) or 0)

    # ------------------------------------------------------------ exclusions
    @property
    def exclusion_titles(self) -> list[str]:
        return [t.lower() for t in (self.raw.get("exclusions") or {}).get("title_terms") or []]

    @property
    def off_domain_titles(self) -> list[str]:
        return [
            t.lower()
            for t in (self.raw.get("exclusions") or {}).get("off_domain_titles") or []
        ]

    # ------------------------------------------------------------- llm views
    @property
    def visa_note(self) -> str:
        return " ".join((self.preferences.get("visa_note") or "").split())

    def geography_rule(self) -> str:
        """Built from the actual configured onsite cities/countries, not a
        hardcoded city name — see repo audit 2026-09-07 finding #4: the LLM
        prompt used to hardcode "Hyderabad only" independently of
        `preferences.onsite_cities`, so editing profile.yaml silently stopped
        matching what the LLM was actually told."""
        cities = [c for group in self.onsite_cities for c in group]
        city_text = ", ".join(cities) if cities else "no onsite/hybrid city configured"
        abroad_text = (
            f"; onsite/hybrid is also in scope in: {', '.join(self.onsite_countries)}"
            if self.onsite_countries else ""
        )
        # Search markets are not residence or work authorisation.
        home = self.preferences.get("home_country", "India")
        remote_scope = home.title()
        return (
            f"Geography (strict): REMOTE roles only if the employer accepts "
            f"applicants based in {remote_scope} — a role advertised as remote "
            f"but tied to a single other country does NOT count. ONSITE/HYBRID "
            f"only in: {city_text}{abroad_text}. Any other on-site location is "
            f"out of scope."
        )

    def role_scope_rule(self) -> str:
        """Built from `preferences.qa_roles_only` — see repo audit 2026-09-07
        finding #4: the LLM prompt used to hardcode 'ONLY QA ROLES'
        unconditionally, ignoring the documented `qa_roles_only: false`
        option (config.yaml)."""
        if self.qa_roles_only:
            return (
                "Role scope (strict): ONLY QA/testing/quality/automation/"
                "evaluation roles are in scope. A software developer role is "
                "NOT in scope even when its description mentions testing "
                "heavily."
            )
        return (
            "Role scope: QA/testing/automation/evaluation roles are the "
            "primary target, but relevant software engineering roles with "
            "real testing, quality-engineering, or AI/LLM evaluation "
            "responsibility are also in scope — score them on their actual "
            "responsibilities, not their title alone."
        )

    def llm_block(self) -> str:
        """Compact candidate description handed to the scoring model."""
        exp = self.experience
        prefs = self.preferences
        pay = (
            f"Current comp: {prefs.get('current_ctc_lpa','?')} LPA · "
            f"seeking {prefs.get('min_ctc_lpa','?')}-{prefs.get('target_ctc_lpa','?')} LPA "
            f"(will not move below {prefs.get('min_ctc_lpa','?')} LPA)"
        )
        if prefs.get("compensation_filter_enabled") is False:
            pay = "Compensation is not a selection criterion. Do not filter, penalize or rank jobs by pay."
        block = [
            "CANDIDATE PROFILE",
            f"Headline: {self.headline}",
            f"Experience: ~{exp.get('years','?')} years "
            f"(level: {self.self_level.replace('_',' ')})",
            pay,
            self.geography_rule(),
            self.role_scope_rule(),
        ]
        if self.visa_note:
            block.append(f"Work authorisation: {self.visa_note}")
        if self.experience.get("target_minimum_years"):
            low, high = self.experience["target_minimum_years"]
            block.append(f"Target postings with a stated minimum of {low}-{high} years of relevant experience; do not invent missing requirements.")
        block += ["", self.summary, "",
                  f"Known gaps (do not invent these): {', '.join(self.gaps[:14])}"]
        return "\n".join(block) + "\n"


def load_profile(path: str | Path | None = None) -> Profile:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        raise FileNotFoundError(f"profile.yaml not found at {p}")
    return Profile(raw=yaml.safe_load(p.read_text(encoding="utf-8")) or {})
