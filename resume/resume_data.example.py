"""EXAMPLE / TEMPLATE — copy this to resume_data.py and fill in your own
details. resume_data.py is gitignored (it's your real name, phone, email,
and work history), so a fresh checkout has none until you create one from
this file (repo audit 2026-09-07 finding #12: before this split, the
entire resume/ directory — including the generic docx-building CODE, not
just the personal data — was gitignored, so `apply-kit` could not even be
imported on a fresh checkout).

    cp resume/resume_data.example.py resume/resume_data.py

Then edit every value below. `build_resume.py` and `tailor.py` are generic
and need no changes — they just render whatever this file provides.
"""

NAME = "YOUR NAME"
TAGLINE = "PLACEHOLDER — e.g. 'QA Engineer | Test Automation | AI Testing'"
CONTACT = [
    "PLACEHOLDER — City, Country | Open to remote roles worldwide",
    "PLACEHOLDER — +CC phone | you@example.com",
    "PLACEHOLDER — linkedin.com/in/you | github.com/you",
]

SUMMARY = (
    "PLACEHOLDER — 3-6 sentences summarizing your real experience. This is "
    "what appears at the top of every generated resume; be specific and "
    "honest, since tailor.py only ever reorders this content, never invents "
    "skills beyond what's here."
)

SKILLS = [
    ("PLACEHOLDER — Skill Group Name",
     "comma-separated tools/skills in this group"),
    ("Another Skill Group",
     "more tools/skills"),
]

EXPERIENCE = [
    {
        "title": "PLACEHOLDER — Your Job Title",
        "company": "PLACEHOLDER — Employer Name",
        "dates": "Mon YYYY - Present",
        "bullets": [
            "PLACEHOLDER — one accomplishment/responsibility per bullet, "
            "specific enough that tailor.py's keyword-overlap ranking has "
            "something real to reorder.",
        ],
    },
]

EDUCATION = [
    ("PLACEHOLDER — Degree Name", "Institution, Country | YYYY - YYYY"),
]
