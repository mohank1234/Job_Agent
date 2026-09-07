"""Role classification + deterministic scoring.

Stage 1 of the pipeline. Free, offline, runs over EVERY stored job — no
posting is ever dropped here, only labelled.

The unit of matching is the ROLE, not the technology. "Selenium" is a signal
that tells you how well the candidate matches a role that has already been
identified; it is never itself a role. Titles are matched semantically by
family, so "Senior Quality Assurance Engineer", "Senior Software QA Engineer"
and "Sr. QA Engineer" all land in the same place without an exact-title list.

Everything candidate-specific comes from profile.yaml. What lives here is the
structure of the job market: which titles denote which role family, and which
seniority words denote which level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Job
from .profile import SENIORITY_LADDER, Profile

# ---------------------------------------------------------------------------
# Title vocabulary
# ---------------------------------------------------------------------------

def _any(*alts: str) -> re.Pattern:
    return re.compile("|".join(alts), re.I)


# A testing/quality ROLE in the title. Deliberately broad — this is the
# permissive stage; precision comes from the description evidence and the LLM.
RE_QA = _any(
    r"\bq\.?\s?a\.?\b",
    r"\bquality assurance\b",
    r"\bquality engineer",
    r"\bquality engineering\b",
    r"\bsoftware quality\b",
    r"\bquality analyst\b",
    r"\bquality specialist\b",
    r"\bquality automation\b",
)
RE_TEST = _any(
    r"\btest(ing)? engineer\b",
    r"\btest engineering\b",
    r"\btester\b",
    r"\btest analyst\b",
    r"\btest architect\b",
    r"\btest lead\b",
    r"\btest automation\b",
    r"\bautomation test",
    r"\btest specialist\b",
    r"\btest consultant\b",
    r"\bin test\b",
    r"\btest infrastructure\b",
    r"\btest platform\b",
    r"\btesting\b",
    r"\btest developer\b",
    r"\bvalidation engineer\b",
    r"\bverification engineer\b",
    r"\btest tooling\b",
)
RE_SDET = _any(
    r"\bsdet\b",
    r"\bsoftware (development |developer |design )?engineer in test\b",
    r"\bsoftware developer in test\b",
    r"\bdevelopment engineer in test\b",
)
RE_AUTOMATION = _any(r"\bautomation\b", r"\bautomated\b")
RE_EVAL = _any(
    r"\bevaluation(s)?\b",
    r"\bevals?\b",
    r"\bvalidation\b",
    r"\bbenchmark(ing)?\b",
    r"\bred[ -]?team",
    r"\bverification\b",
)
RE_SWE = _any(
    r"\bsoftware engineer\b",
    r"\bsoftware developer\b",
    r"\bsoftware development engineer\b",
    r"\bsde\b",
    r"\bswe\b",
    r"\bbackend engineer\b",
    r"\bback[- ]end engineer\b",
    r"\bfull[- ]?stack engineer\b",
    r"\bplatform engineer\b",
    r"\bapplication engineer\b",
    r"\bdeveloper\b",
    r"\bprogrammer\b",
)

# AI/ML domain markers in the TITLE.
RE_LLM = _any(r"\bllms?\b", r"\blarge language model", r"\bgen[- ]?ai\b", r"\bgenerative ai\b", r"\bfoundation model", r"\bprompt\b")
RE_RAG = _any(r"\brag\b", r"\bretrieval[- ]augmented", r"\bsemantic search\b", r"\bvector search\b")
RE_AGENT = _any(r"\bagent(s|ic)?\b", r"\bcopilot\b", r"\bagentic\b", r"\btool[- ]calling\b")
RE_CHAT = _any(r"\bchat ?bot", r"\bconversational\b", r"\bvirtual assistant\b", r"\bai assistant\b", r"\bvoice assistant\b", r"\bdialog(ue)?\b")
RE_CV = _any(r"\bcomputer vision\b", r"\bcv\b", r"\bvision model", r"\bimage (recognition|classification)\b", r"\bobject detection\b", r"\bocr\b", r"\bperception\b")
RE_AI = _any(r"\bai\b", r"\ba\.i\.\b", r"\bml\b", r"\bm\.l\.\b", r"\bmachine learning\b", r"\bartificial intelligence\b", r"\bmodel(s)?\b", r"\bnlp\b", r"\bdeep learning\b")

# "Quality" that is not software quality. Guards against pharma/manufacturing/
# data-quality roles matching RE_QA.
RE_NOT_SOFTWARE_QUALITY = _any(
    r"\bdata quality\b",
    r"\bsupplier quality\b",
    r"\bmanufacturing quality\b",
    r"\bquality control\b",
    r"\bquality inspector\b",
    r"\bquality manager\b",
    r"\bquality technician\b",
    r"\bclinical\b",
    r"\bpharma",
    r"\bgmp\b",
    r"\bfood safety\b",
    r"\bwelding\b",
    r"\biso 9001\b",
    r"\bconstruction\b",
    r"\bmechanical\b",
    r"\bautomotive quality\b",
    r"\bhardware\b",
    r"\bsemiconductor\b",
    r"\bwafer\b",
    r"\bsilicon\b",
    r"\bpcb\b",
    r"\bmanufactur",
    r"\bdesign verification\b",
    r"\binterconnect\b",
    r"\belectrical\b",
    r"\bthermal\b",
    r"\boptical\b",
    r"\bassembly\b",
    r"\bcalibration\b",
    r"\btechnician\b",
    r"\bcall quality\b",
    r"\bcontact cent(er|re)\b",
    r"\bcall cent(er|re)\b",
    r"\bcustomer (service|support) quality\b",
    r"\bair quality\b",
)

# Markers of industrial / factory QC, matched against the BODY rather than the
# title. Singapore advertises shop-floor quality work as plain "QA Engineer",
# so the title alone cannot separate it from software testing - the giveaway is
# in the description. Only consulted when the posting shows no software-testing
# evidence at all, so a genuine software role at a manufacturer is unaffected.
RE_INDUSTRIAL_QUALITY = _any(
    r"\bqa ?/ ?qc\b",
    r"\bqc inspection\b",
    r"\bincoming inspection\b",
    r"\bnon-?conformance\b",
    r"\bcorrective and preventive action\b",
    r"\bcapa\b",
    r"\biso ?(9001|13485|14001)\b",
    r"\bgood manufacturing practice\b",
    r"\bproduction line\b",
    r"\bshop ?floor\b",
    r"\braw materials?\b",
    r"\bbatch record",
    r"\bcalibration of (equipment|instruments)\b",
    r"\bwelding\b",
    r"\bmarine\b",
    r"\bshipyard\b",
    r"\boffshore\b",
    r"\bchemical plant\b",
    r"\bmedical device",
    r"\bsterilis|\bsterili z",
    r"\bfood (safety|hygiene)\b",
    r"\bhaccp\b",
    r"\bwafer fab",
    r"\bcleanroom\b",
    r"\bconstruction site\b",
    r"\bcivil works\b",
)

# Seniority words -> ladder level. Ordered: first hit wins.
SENIORITY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("intern", _any(r"\bintern(ship)?\b", r"\btrainee\b", r"\bapprentice\b")),
    ("executive", _any(r"\bchief\b", r"\bcto\b", r"\bcxo\b")),
    ("vp", _any(r"\bvp\b", r"\bvice president\b")),
    ("head", _any(r"\bhead of\b")),
    ("director", _any(r"\bdirector\b")),
    ("manager", _any(r"\bmanager\b", r"\bmanagement\b")),
    ("architect", _any(r"\barchitect\b")),
    ("principal", _any(r"\bprincipal\b", r"\bdistinguished\b", r"\bfellow\b")),
    ("staff", _any(r"\bstaff\b")),
    ("lead", _any(r"\blead\b", r"\bleader\b")),
    ("senior", _any(r"\bsenior\b", r"\bsr\.?\b", r"\biii\b", r"\blevel 3\b", r"\bl[45]\b")),
    ("junior", _any(r"\bjunior\b", r"\bjr\.?\b", r"\bentry[- ]level\b", r"\bgraduate\b", r"\bassociate\b", r"\bfresher\b")),
]

# Role family -> (human label, tier, deterministic base weight).
FAMILIES: dict[str, tuple[str, int, int]] = {
    "core_qa":              ("Core QA / Software Testing",          1, 62),
    "automation_sdet":      ("Test Automation / SDET",              1, 66),
    "ai_llm_qa":            ("AI / LLM Quality & Testing",          1, 70),
    "ai_evaluation":        ("AI / Model Evaluation",               1, 66),
    "rag_agent_qa":         ("RAG / AI Agent Testing",              2, 68),
    "conversational_ai_qa": ("Chatbot / Conversational AI Testing", 2, 64),
    "computer_vision_qa":   ("Computer Vision / Model Testing",     2, 60),
    "swe_quality":          ("SWE — Test / Quality / Tooling",      2, 60),
    "swe_ai":               ("SWE — AI / LLM Applications",         3, 46),
    "swe_generic":          ("SWE — general",                       3, 34),
    "off_domain":           ("Different job family",                4, 16),
    "excluded":             ("Excluded by profile rules",           4, 6),
    "other":                ("Unclassified",                        4, 14),
}

# Description evidence weights: (max points, points per distinct term).
EVIDENCE_WEIGHTS: dict[str, tuple[float, float]] = {
    "qa":              (6.0, 0.7),
    "automation":      (6.0, 0.8),
    "api":             (5.0, 0.8),
    "ci_cd":           (2.5, 0.5),
    "ai_llm":          (5.0, 0.8),
    "evaluation":      (6.0, 0.9),
    "rag":             (3.5, 0.9),
    "agents":          (3.5, 0.9),
    "conversational":  (2.5, 0.8),
    "computer_vision": (2.5, 0.8),
    "programming":     (2.5, 0.6),
    "ai_assisted_dev": (3.0, 0.9),
}

RE_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(\d{1,2})?\s*\+?\s*(?:years?|yrs?)"
    r"(?:[^.]{0,40}?(?:experience|exp\b))?",
    re.I,
)


@dataclass
class Classification:
    family: str = "other"
    label: str = "Unclassified"
    tier: int = 4
    seniority: str = "mid"
    base_score: int = 0
    candidate: bool = False
    evidence: dict[str, list[str]] = field(default_factory=dict)
    gap_terms: list[str] = field(default_factory=list)
    years_required: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def domains(self) -> list[str]:
        """Skill groups with real evidence, strongest first."""
        return [k for k, v in sorted(self.evidence.items(), key=lambda kv: -len(kv[1])) if v]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def detect_seniority(title: str) -> str:
    for level, pattern in SENIORITY_PATTERNS:
        if pattern.search(title):
            return level
    return "mid"


def detect_years_required(text: str) -> float | None:
    """Smallest plausible 'N years experience' requirement stated in the text."""
    found: list[float] = []
    for match in RE_YEARS.finditer(text[:6000]):
        low = match.group(1)
        try:
            value = float(low)
        except (TypeError, ValueError):
            continue
        if 0 < value <= 20:
            found.append(value)
    return min(found) if found else None


_TERM_CACHE: dict[str, re.Pattern] = {}


def _term_re(term: str) -> re.Pattern:
    """Word-boundary matcher for a skill term.

    Plain substring matching is wrong for short terms: "rag" matches inside
    "storage" and "average", "git" inside "digital". Every term is anchored.
    """
    pattern = _TERM_CACHE.get(term)
    if pattern is None:
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", re.I)
        _TERM_CACHE[term] = pattern
    return pattern


# Keeps the Profile alive alongside its patterns, so an id() key can never be
# reused by a different profile allocated at the same address.
_GROUP_CACHE: dict[int, tuple[Profile, dict[str, re.Pattern]]] = {}

# Descriptions average ~11 KB. Everything that decides a match — title,
# responsibilities, requirements — is near the top; scanning the boilerplate
# tail costs 10x for no signal.
EVIDENCE_SCAN_CHARS = 6000


def _group_patterns(profile: Profile) -> dict[str, re.Pattern]:
    """One combined alternation per skill group.

    260 separate scans over an 11 KB description is ~250s across a full fetch;
    one pass per group is ~25s. Longest alternatives first so "test automation"
    wins over "test" at the same position.
    """
    key = id(profile)
    entry = _GROUP_CACHE.get(key)
    if entry is None:
        cached: dict[str, re.Pattern] = {}
        groups = dict(profile.skills)
        groups["__gaps__"] = profile.gaps
        for group, terms in groups.items():
            if not terms:
                continue
            alts = "|".join(
                re.escape(t) for t in sorted(set(terms), key=len, reverse=True)
            )
            cached[group] = re.compile(
                rf"(?<![a-z0-9])({alts})(?![a-z0-9])", re.I
            )
        entry = (profile, cached)
        _GROUP_CACHE[key] = entry
    return entry[1]


def gather_evidence(job: Job, profile: Profile) -> tuple[dict[str, list[str]], list[str]]:
    """Which of the candidate's skill groups the posting actually talks about."""
    hay = f" {job.title.lower()} {job.description[:EVIDENCE_SCAN_CHARS].lower()} "
    patterns = _group_patterns(profile)

    evidence: dict[str, list[str]] = {}
    gaps: list[str] = []
    for group, pattern in patterns.items():
        hits = sorted({m.group(1).lower() for m in pattern.finditer(hay)})
        if not hits:
            continue
        if group == "__gaps__":
            gaps = hits
        else:
            evidence[group] = hits
    return evidence, gaps


def _title_family(
    title: str,
    evidence: dict[str, list[str]],
    profile: Profile,
    body: str = "",
) -> tuple[str, list[str]]:
    """Semantic role family from the title, with the description as tiebreak."""
    notes: list[str] = []

    # Word-anchored: a substring test excludes "Enterprise QA Engineer,
    # Salesforce" because "sales" appears inside "Salesforce".
    hit = next((t for t in profile.exclusion_titles if _term_re(t).search(title)), None)
    if hit:
        return "excluded", [f"title matches profile exclusion '{hit}'"]

    is_qa = bool(RE_QA.search(title))
    is_test = bool(RE_TEST.search(title))
    is_sdet = bool(RE_SDET.search(title))
    testing_role = is_qa or is_test or is_sdet

    if RE_NOT_SOFTWARE_QUALITY.search(title):
        return "off_domain", ["quality/test role outside software"]

    # A bare "QA Engineer" title says nothing about the domain. In India it is
    # software by default; in Singapore the same words usually mean factory,
    # marine, chemical or semiconductor quality control. So when the title is
    # ambiguous, decide on the body: industrial markers with no software-testing
    # evidence at all means this is not our job family.
    if testing_role and not evidence and body and RE_INDUSTRIAL_QUALITY.search(body):
        return "off_domain", [
            "quality role in manufacturing/industrial QC, not software testing"
        ]

    # A different job family wins over any AI/eval wording in the title, unless
    # the title also names a testing role. Without this, "User Researcher, AI
    # Evaluations" and "Machine Learning Engineer, Eval" are scored as AI
    # evaluation engineering roles, which they are not.
    if not testing_role:
        off = next(
            (t for t in profile.off_domain_titles if _term_re(t).search(title)), None
        )
        if off:
            return "off_domain", [f"title belongs to a different job family ('{off}')"]

    has_llm = bool(RE_LLM.search(title))
    has_rag = bool(RE_RAG.search(title))
    has_agent = bool(RE_AGENT.search(title))
    has_chat = bool(RE_CHAT.search(title))
    has_cv = bool(RE_CV.search(title))
    has_ai = bool(RE_AI.search(title)) or has_llm or has_rag or has_agent or has_chat or has_cv

    # Which AI sub-domain the posting is really about. The title decides when
    # it names one; otherwise the description's evidence does.
    def ai_subdomain() -> str | None:
        if has_cv or len(evidence.get("computer_vision", [])) >= 2:
            return "computer_vision_qa"
        if has_chat or len(evidence.get("conversational", [])) >= 2:
            return "conversational_ai_qa"
        if has_rag or has_agent or len(evidence.get("rag", [])) >= 2 \
                or len(evidence.get("agents", [])) >= 2:
            return "rag_agent_qa"
        return None

    if testing_role:
        if has_ai:
            sub = ai_subdomain()
            if sub:
                return sub, [f"{FAMILIES[sub][0]} role"]
            return "ai_llm_qa", ["AI/LLM quality role"]
        if is_sdet or RE_AUTOMATION.search(title):
            return "automation_sdet", ["automation / SDET role"]
        return "core_qa", ["core QA / testing role"]

    # Evaluation-flavoured AI roles that never say "QA" or "test".
    if RE_EVAL.search(title) and has_ai:
        sub = ai_subdomain()
        if sub:
            return sub, [f"{FAMILIES[sub][0]} evaluation role"]
        return "ai_evaluation", ["AI/model evaluation role"]

    if RE_SWE.search(title):
        qa_ev = len(evidence.get("qa", [])) + len(evidence.get("automation", []))
        eval_ev = len(evidence.get("evaluation", []))
        api_ev = len(evidence.get("api", []))
        if qa_ev >= 3 or eval_ev >= 3:
            return "swe_quality", ["SWE role whose responsibilities are testing/quality work"]
        if has_ai and (qa_ev >= 2 or eval_ev >= 2 or api_ev >= 2):
            return "swe_ai", ["AI-application SWE role with testing/eval overlap"]
        if has_ai:
            return "swe_ai", ["AI-application SWE role; overlap unproven"]
        if qa_ev >= 2 or eval_ev >= 2:
            return "swe_generic", ["general SWE role with some quality overlap"]
        notes.append("general SWE role with no testing/quality overlap")
        return "off_domain", notes

    # (off_domain_titles was already checked above, before the AI/eval branch.)

    # No role signal in the title at all — let the description decide whether
    # it is worth keeping. Permissive by design.
    qa_ev = len(evidence.get("qa", [])) + len(evidence.get("automation", []))
    if qa_ev >= 5 or len(evidence.get("evaluation", [])) >= 4:
        return "core_qa", ["untitled role, but the responsibilities are QA work"]

    return "other", ["no recognisable role signal"]


def _seniority_delta(job_level: str, profile: Profile) -> tuple[float, str | None]:
    """Flexible seniority fit. One level up is fine; far above is not."""
    try:
        job_index = SENIORITY_LADDER.index(job_level)
    except ValueError:
        job_index = SENIORITY_LADDER.index("mid")
    delta = job_index - profile.self_level_index

    if job_level in profile.reject_levels:
        return -26.0, f"role is {job_level} level — substantially above current scope"
    if job_level == "intern":
        return -30.0, "internship / trainee"
    if delta <= -2:
        return -9.0, "role is below current level"
    if delta <= 0:
        return 0.0, None
    if delta == 1:
        return 2.0, None                       # the natural next step
    if delta == 2:
        return -5.0, "one level above current — stretch, still worth it on a strong match"
    return -13.0, "seniority materially above current level"


def _experience_delta(years_required: float | None, profile: Profile) -> tuple[float, str | None]:
    """Experience is FLEXIBLE. A small gap costs a little; it never rejects."""
    if years_required is None:
        return 0.0, None
    gap = years_required - profile.years
    if gap <= 0:
        return 3.0, None
    if gap <= profile.stretch_years:
        return -1.5 * gap, f"asks for {years_required:.0f}y vs ~{profile.years:.0f}y — small, closable gap"
    if gap <= profile.hard_gap_years:
        return -3.5 * gap, f"asks for {years_required:.0f}y vs ~{profile.years:.0f}y — meaningful gap"
    return -22.0, f"asks for {years_required:.0f}y vs ~{profile.years:.0f}y — large gap"


INDIA_MARKERS = ("india", "bengaluru", "bangalore", "hyderabad", "secunderabad",
                 "pune", "chennai", "mumbai", "delhi", "noida", "gurgaon",
                 "gurugram", "kolkata", "ahmedabad", "kochi", "coimbatore",
                 "jaipur", "indore", "bhubaneswar", "trivandrum")

# "Remote" on a job board almost never means "remote from anywhere". Most
# postings marked remote are remote WITHIN one country, which is useless to
# someone applying from India. These decide which kind you are looking at.
RE_REMOTE_WORDS = re.compile(
    r"\b(fully\s+)?remote(\s*[-–|,:]?\s*(first|friendly|ok|optional|eligible))?\b|"
    r"\bwork from home\b|\bwfh\b|\bdistributed\b|\bhybrid\b|\bonsite\b|\bon-site\b",
    re.I,
)
# For the LOCATION field, where "Global" genuinely means global.
RE_WORLDWIDE = re.compile(
    r"\b(worldwide|world ?wide|anywhere|any location|any country|global(ly)?|"
    r"international|no location restriction|location[- ]independent|"
    r"work from anywhere)\b", re.I,
)
# For the DESCRIPTION BODY, where loose words are worthless: almost every
# posting says "our global team" or "international clients". Only phrases
# that actually promise location independence count here.
RE_WORLDWIDE_BODY = re.compile(
    r"(work from anywhere|anywhere in the world|from any country|"
    r"no location restrictions?|location[- ]independent|"
    r"hire (from )?(anywhere|globally)|fully distributed (team|company)|"
    r"any time ?zone|globally distributed team|"
    r"open to candidates (from )?anywhere)", re.I,
)
RE_APAC = re.compile(r"\b(apac|asia[- ]pacific|asia|emea[/ ]?apac)\b", re.I)

# Phrases in the body that pin a "remote" role to one country.
RE_GEO_RESTRICTION = re.compile(
    r"(must (be |reside |live )?(located |based |reside |living )?in\b"
    r"|must be (a )?(us|u\.s\.|uk|canadian|australian) (citizen|resident)"
    r"|authorized to work in\b"
    r"|eligible to work in\b"
    r"|work authorization in\b"
    r"|legally authorized to work"
    r"|residents? of\b"
    r"|within the (united states|us|uk|eu|european union|country)"
    r"|this (role|position) is (fully )?remote (with)?in\b"
    r"|remote (with)?in the\b"
    r"|only considering candidates (located |based )?in\b"
    r"|candidates must be (located|based)\b)", re.I,
)


def remote_eligibility(job: Job) -> tuple[str, str | None]:
    """Can someone in India actually take this remote job?

    Returns (verdict, note) where verdict is:
      open          -> worldwide, or explicitly India/APAC
      region_locked -> remote, but tied to another country
      unspecified   -> says only "remote", nothing more to go on
      n/a           -> not a remote posting
    """
    if job.workplace != "remote":
        return "n/a", None

    location = (job.location or "").lower()
    head = (job.description or "")[:1500].lower()

    # 1. The location field is the strongest signal and is checked first. A
    #    named country there beats anything the marketing copy claims.
    if any(m in location for m in INDIA_MARKERS) or RE_APAC.search(location):
        return "open", None
    if RE_WORLDWIDE.search(location):
        return "open", None

    # 2. Strip the remote/hybrid words out of the location. Whatever is left is
    #    a place: "Brazil - Remote" -> "brazil", "USA | Remote" -> "usa".
    remainder = RE_REMOTE_WORDS.sub(" ", location)
    remainder = re.sub(r"[^a-z ]+", " ", remainder)
    remainder = " ".join(remainder.split())
    if remainder and len(remainder) > 2:
        return "region_locked", (
            f"advertised as remote but tied to {job.location} — "
            f"not open to applicants based in India"
        )

    # 3. Location said nothing useful. Fall back to the body, but only on
    #    phrases that actually promise location independence.
    if any(m in head for m in INDIA_MARKERS):
        return "open", None
    if RE_GEO_RESTRICTION.search(head):
        return "region_locked", (
            "the description restricts this remote role to one country's residents"
        )
    if RE_WORLDWIDE_BODY.search(head):
        return "open", None
    return "unspecified", None


# --- Singapore work authorisation ------------------------------------------
# An onsite Singapore role is only real if the employer sponsors an Employment
# Pass. Postings rarely say "we sponsor" outright, but they very often say the
# opposite, and MOM's salary floor settles the rest: below it, sponsorship is
# not legally available however willing the employer is.
RE_SG = re.compile(r"(?<![a-z])(singapore|singapura)(?![a-z])", re.I)

RE_SG_LOCALS_ONLY = _any(
    r"\bsingaporeans? (only|and pr|& pr|/ ?pr|or pr)",
    r"\b(sc|singapore citizens?) ?(/|and|&|or) ?pr\b",
    r"\bpermanent residents? only\b",
    r"\blocal (candidates?|applicants?|hires?) only\b",
    r"\bmust (already )?(have|possess|hold) (a )?valid (work|employment) (pass|permit|visa)\b",
    r"\bno (visa |employment pass |ep )?sponsorship\b",
    r"\bsponsorship (is )?not (available|provided|offered)\b",
    r"\bnot able to sponsor\b",
    r"\beligible to work in singapore without sponsorship\b",
    r"\bonly singaporeans\b",
)

RE_SG_SPONSORS = _any(
    r"\b(visa|employment pass|ep|work pass) sponsorship\b",
    r"\bwe (will )?sponsor\b",
    r"\bsponsorship (is )?(available|provided|offered)\b",
    r"\bemployment pass will be (provided|arranged|applied)\b",
    r"\bwilling to sponsor\b",
    r"\bopen to (foreign|overseas|international) (candidates|applicants|talent)\b",
    r"\brelocation (support|assistance|package|provided)\b",
    r"\bwe support relocation\b",
)

# The MyCareersFuture source writes this line when it knows the salary.
RE_SG_SALARY_FLOOR = re.compile(
    r"\[work authorisation: monthly salary [^]]*?\b(meets|below)\b", re.I)


def singapore_eligibility(job: Job, profile: Profile) -> tuple[str, str | None]:
    """Can this Singapore role actually be taken by someone needing a visa?

    Returns one of:
      n/a                 not a Singapore posting
      sponsors            explicit sponsorship-intent language found
      salary_meets_floor  pays at/above the EP floor, but no explicit
                           sponsorship language — financially eligible,
                           intent unconfirmed (see note below)
      locals_only         explicitly excludes anyone needing sponsorship
      below_floor         pays under the EP floor, so an EP cannot be granted
      unspecified         Singapore, but silent on both salary and sponsorship

    Compensation and sponsorship-intent are two distinct real-world signals —
    a company can pay well above the floor and still only hire locals, or
    simply not have decided. Before this fix (repo audit 2026-09-07 finding
    #17), meeting the salary floor alone returned "sponsors", which then
    scored identically to an employer that had actually said it would
    sponsor. `salary_meets_floor` keeps that distinction visible downstream
    instead of silently upgrading a compensation fact into a confirmed
    sponsorship claim.
    """
    blob = f"{job.location or ''} {job.company or ''}".lower()
    text = (job.description or "")[:EVIDENCE_SCAN_CHARS]
    if not RE_SG.search(blob) and not RE_SG.search(text[:400]):
        return "n/a", None

    if RE_SG_LOCALS_ONLY.search(text):
        return "locals_only", (
            "Singapore role that excludes candidates needing sponsorship - "
            "you would need an Employment Pass"
        )

    # Explicit sponsorship-intent language is checked FIRST and independently
    # of salary — a posting can state both, and the explicit statement is the
    # stronger signal either way.
    if RE_SG_SPONSORS.search(text):
        return "sponsors", None

    m = RE_SG_SALARY_FLOOR.search(text)
    if m:
        if m.group(1).lower() == "below":
            return "below_floor", (
                f"Singapore salary is under the Employment Pass floor of "
                f"S${profile.ep_min_monthly_sgd:,.0f}/month, so no employer "
                f"can sponsor this one"
            )
        return "salary_meets_floor", (
            f"Singapore salary meets the Employment Pass floor of "
            f"S${profile.ep_min_monthly_sgd:,.0f}/month, so sponsorship is "
            f"legally possible - but nothing in the posting says the "
            f"employer will actually sponsor. Confirm intent before "
            f"investing time."
        )

    return "unspecified", (
        "Singapore role that does not say whether it sponsors an Employment "
        "Pass - worth one email before you invest time in applying"
    )


def _location_delta(job: Job, profile: Profile) -> tuple[float, str | None]:
    """Work-location policy. Strict.

      REMOTE  — in scope only if someone in India can actually take it. A role
                marked "remote" but tied to the US, Canada, Brazil or anywhere
                else is NOT applicable and is scored down hard.
      ONSITE / HYBRID — in scope only in the cities in profile.yaml
                (Hyderabad). Anywhere else, in India or abroad, is out.

    Nothing is deleted: out-of-scope postings stay in the database at a low
    score so you can still see what was considered.
    """
    blob = f"{job.location} {job.workplace}".lower()

    if job.workplace == "remote":
        verdict, note = remote_eligibility(job)
        if verdict == "open":
            return 8.0, None
        if verdict == "region_locked":
            return -30.0, note
        # Says "remote" and nothing else. Plausible but unproven.
        return 1.0, "remote, but the posting does not say which countries it accepts"

    if job.workplace == "unknown" or not blob.strip():
        return 0.0, None

    # Onsite / hybrid. Ranked city groups; the first group is the preference.
    for rank, group in enumerate(profile.onsite_cities):
        if any(city in blob for city in group):
            return (8.0 if rank == 0 else 5.0 - rank), None

    # Singapore is the one country abroad where onsite is in scope, and only
    # when the employer can actually sponsor an Employment Pass.
    if "singapore" in profile.onsite_countries:
        verdict, note = singapore_eligibility(job, profile)
        if verdict == "sponsors":
            return 7.0, None
        if verdict == "salary_meets_floor":
            return 4.0, note   # financially eligible, intent unconfirmed
        if verdict == "locals_only":
            return -30.0, note
        if verdict == "below_floor":
            return -28.0, note
        if verdict == "unspecified":
            return 1.0, note

    if any(m in blob for m in INDIA_MARKERS):
        return -26.0, (
            f"onsite/hybrid in {job.location or 'India'} — only Hyderabad is "
            f"in scope for onsite or hybrid work"
        )
    return -34.0, (
        f"onsite/hybrid in {job.location or 'an unlisted location'} — outside "
        f"India; only fully remote roles open to India are in scope abroad"
    )


# Staffing and recruitment firms post on behalf of an unnamed client. The role
# is real but the employer is not the one you would be joining, the pay band is
# usually lower, and the process is a black box. Word-anchored so "Consulting"
# inside a product company's name does not trip it.
RE_STAFFING = _any(
    r"\bconsultanc(y|ies)\b",
    r"\bconsulting (solutions|services|group|inc|llc|pvt)\b",
    r"\bmanpower\b",
    r"\bstaffing\b",
    r"\brecruit(ment|ers|ing)\b",
    r"\bplacements?\b",
    r"\bhr (solutions|services|consultanc)",
    r"\btalent (solutions|acquisition|search)\b",
    r"\bresourcing\b",
    r"\bhiring solutions\b",
    r"\bworkforce solutions\b",
    r"\bhead ?hunt",
    r"\bjob ?consultan",
    r"\bmanagement consultan",
    r"\bhuman resource",
)


def _company_delta(job: Job, profile: Profile) -> tuple[float, str | None]:
    """Employer quality.

    A QA role at a product company that pays 30+ LPA is worth more than the
    same role posted by a staffing agency for an unnamed client. This is what
    separates the two.
    """
    company = (job.company or "").lower().strip()
    if not company:
        return 0.0, None

    # Precedence is explicit-agency, then preferred, then the fuzzy pattern.
    # A firm named in staffing_companies must never be rescued by a preferred
    # name that happens to be a substring of it, and conversely a preferred
    # company must not be penalised just because the generic pattern fires on
    # a word in its name.
    if any(_term_re(a).search(company) for a in profile.staffing_companies):
        return -profile.staffing_agency_penalty, (
            f"posted by {job.company} - a staffing/recruitment firm, not the "
            f"employer; the actual company and pay band are undisclosed"
        )

    for preferred in profile.preferred_companies:
        if _term_re(preferred).search(company):
            return profile.preferred_company_bonus, None

    if RE_STAFFING.search(company):
        return -profile.staffing_agency_penalty, (
            f"posted by {job.company} - a staffing/recruitment firm, not the "
            f"employer; the actual company and pay band are undisclosed"
        )
    return 0.0, None


def _evidence_points(evidence: dict[str, list[str]]) -> float:
    total = 0.0
    for group, hits in evidence.items():
        cap, per = EVIDENCE_WEIGHTS.get(group, (1.0, 0.4))
        total += min(cap, per * len(hits))
    return total


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify(job: Job, profile: Profile) -> Classification:
    """Label one posting. Never drops it — tier 4 is a label, not a delete."""
    title = job.title.lower()
    evidence, gap_terms = gather_evidence(job, profile)
    family, notes = _title_family(
        title, evidence, profile, (job.description or "")[:EVIDENCE_SCAN_CHARS].lower()
    )
    label, tier, weight = FAMILIES[family]

    seniority = detect_seniority(title)
    years_required = detect_years_required(job.description)

    score = float(weight)
    score += _evidence_points(evidence)

    sen_pts, sen_note = _seniority_delta(seniority, profile)
    score += sen_pts
    if sen_note:
        notes.append(sen_note)

    exp_pts, exp_note = _experience_delta(years_required, profile)
    score += exp_pts
    if exp_note:
        notes.append(exp_note)

    loc_pts, loc_note = _location_delta(job, profile)
    score += loc_pts
    if loc_note:
        notes.append(loc_note)

    comp_pts, comp_note = _company_delta(job, profile)
    score += comp_pts
    if comp_note:
        notes.append(comp_note)

    if gap_terms:
        score -= min(8.0, 1.0 * len(gap_terms))

    # Tier 3 SWE roles must earn their place — a generic SWE posting should
    # never outrank a real QA posting on keyword count alone.
    if family in ("swe_generic", "swe_ai") and not evidence.get("qa") and not evidence.get("evaluation"):
        score -= 10.0
        notes.append("generic SWE responsibilities — little overlap with QA/eval work")

    classification = Classification(
        family=family,
        label=label,
        tier=tier,
        seniority=seniority,
        base_score=int(max(0, min(100, round(score)))),
        candidate=tier <= 3,
        evidence=evidence,
        gap_terms=gap_terms,
        years_required=years_required,
        notes=notes,
    )
    return classification


def classify_all(jobs: list[Job], profile: Profile) -> list[Job]:
    """Classify in place. Returns the same list — nothing is removed."""
    for job in jobs:
        job.apply_classification(classify(job, profile))
    return jobs


# Families where the TITLE names a testing/quality/evaluation role. These are
# the only families that survive `qa_roles_only`. Everything else — including
# "Software Engineer, Generative AI" and "Senior Software Engineer, Agents" —
# is a developer job, however much testing its description happens to mention.
QA_FAMILIES = frozenset({
    "core_qa", "automation_sdet", "ai_llm_qa", "ai_evaluation",
    "rag_agent_qa", "conversational_ai_qa", "computer_vision_qa",
})


OUT_OF_SCOPE_CAP = 40          # category D — visible in the database, never shown


def scope_cap(job: Job, profile: Profile, qa_only: bool = True) -> tuple[int, str | None]:
    """Hard ceiling applied AFTER scoring, whatever the model said.

    The location and role rules are absolute, so they cannot be left to a
    prompt or to a score that was cached under an older policy. This runs last
    and overrides both.
    """
    if qa_only and job.role_family not in QA_FAMILIES:
        return OUT_OF_SCOPE_CAP, (
            f"not a QA role — classified as {job.role_label}; only testing, "
            f"quality and evaluation roles are in scope"
        )

    if job.workplace == "remote":
        verdict, note = remote_eligibility(job)
        if verdict == "region_locked":
            return OUT_OF_SCOPE_CAP, note
        return 100, None

    if job.workplace in ("onsite", "hybrid"):
        blob = f"{job.location} {job.workplace}".lower()
        for group in profile.onsite_cities:
            if any(city in blob for city in group):
                return 100, None

        # Singapore clears the cap only when sponsorship is actually possible,
        # or worth a confirming email. "unspecified" and "salary_meets_floor"
        # are left uncapped on purpose — capping either would hide a role
        # that is still worth one email to confirm, but neither is treated
        # as a confirmed "sponsors" (see singapore_eligibility docstring).
        if "singapore" in profile.onsite_countries:
            verdict, note = singapore_eligibility(job, profile)
            if verdict in ("sponsors", "unspecified", "salary_meets_floor"):
                return 100, None
            if verdict in ("locals_only", "below_floor"):
                return OUT_OF_SCOPE_CAP, note

        return OUT_OF_SCOPE_CAP, (
            f"{job.workplace} in {job.location or 'an unlisted location'} — only "
            f"Hyderabad and Singapore (with Employment Pass sponsorship) are in "
            f"scope for onsite or hybrid work"
        )

    return 100, None


def apply_scope_cap(job: Job, profile: Profile, qa_only: bool = True) -> None:
    """Enforce the cap on a scored job, rewriting category and priority."""
    cap, reason = scope_cap(job, profile, qa_only)
    if job.score <= cap:
        return
    job.score = cap
    job.match_category, job.match_category_label = category_for(job.score)
    job.priority = "LOW"
    if reason:
        job.experience_gap = reason
        job.recommendation = f"Out of scope: {reason}"


def candidates(jobs: list[Job], qa_only: bool = True) -> list[Job]:
    """Candidate set for the expensive stage, best-first.

    With qa_only (the default), a posting must be a QA/testing/evaluation ROLE
    by title. Developer roles that merely mention testing do not qualify.
    """
    picked = [j for j in jobs if j.candidate]
    if qa_only:
        picked = [j for j in picked if j.role_family in QA_FAMILIES]
    picked.sort(key=lambda j: (-j.base_score, j.role_tier))
    return picked


# ---------------------------------------------------------------------------
# Score -> category / priority (shared by the LLM path and the fallback path)
# ---------------------------------------------------------------------------

CATEGORY_BOUNDS = [
    (90, "A", "STRONG MATCH"),
    (80, "B", "VERY GOOD MATCH"),
    (60, "C", "GOOD / ADJACENT MATCH"),
    (40, "D", "LOW MATCH"),
    (0,  "E", "NOT RELEVANT"),
]


def category_for(score: int) -> tuple[str, str]:
    for floor, letter, label in CATEGORY_BOUNDS:
        if score >= floor:
            return letter, label
    return "E", "NOT RELEVANT"


def priority_for(score: int, classification_tier: int, missing: int = 0,
                 freshness_rank: int | None = None) -> str:
    """Application priority: fit first, then how fresh the posting is.

    A good role posted in the last 24 hours beats the same role posted six days
    ago, because the pile the recruiter is reading is still small. Freshness
    can promote a fitting job one step, and demote a week-old one — it can
    never promote a job that does not fit.
    """
    if score >= 82 and classification_tier <= 2:
        base = "HIGH"
    elif score >= 72 and classification_tier <= 2 and missing <= 4:
        base = "HIGH"
    elif score >= 60:
        base = "MEDIUM"
    else:
        base = "LOW"

    if freshness_rank is None or base == "LOW":
        return base

    ladder = ["LOW", "MEDIUM", "HIGH"]
    i = ladder.index(base)
    if freshness_rank == 0:          # posted in the last 24 hours
        i = min(len(ladder) - 1, i + 1)
    elif freshness_rank >= 3:        # 4-7 days old, or undated
        i = max(0, i - 1)
    return ladder[i]
