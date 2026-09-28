"""A resume tailored to one job description, as an ATS-readable PDF.

Tailoring only reorders and emphasises what resume/resume_data.py already
says: the headline mirrors the posting's focus, skill groups and skills the
job description names come first, and experience bullets are ordered by how
much they share with it. No skill, tool or claim is ever added.

The ATS score is strict keyword coverage: of the tools and skills the job
description names (a fixed QA/engineering vocabulary plus the resume's own
skill terms), the share that appears word-for-word in the resume. Missing
ones are listed, never inserted.

The PDF is single column with standard section headings and real text (no
tables, images or text boxes), the layout ATS parsers read reliably.
"""
from __future__ import annotations

import ast
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("NAME", "TAGLINE", "CONTACT", "SUMMARY", "SKILLS", "EXPERIENCE", "EDUCATION")
# Tools and skills a QA/SDET posting commonly names. A vocabulary, not a
# claim: a term counts as a match only when the resume itself contains it.
LEXICON = [
    "selenium", "playwright", "cypress", "puppeteer", "webdriverio", "appium", "espresso", "xcuitest",
    "testng", "junit", "pytest", "jest", "mocha", "cucumber", "behave", "robot framework", "karate",
    "rest assured", "postman", "soapui", "graphql", "grpc", "api testing", "microservices",
    "jmeter", "k6", "gatling", "locust", "loadrunner", "performance testing", "load testing",
    "python", "java", "javascript", "typescript", "golang", "c#", ".net", "kotlin", "swift", "ruby",
    "sql", "mysql", "postgresql", "mongodb", "redis", "kafka",
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "linux",
    "jenkins", "github actions", "gitlab", "circleci", "ci/cd", "git",
    "jira", "testrail", "zephyr", "xray", "browserstack", "sauce labs", "lambdatest",
    "agile", "scrum", "bdd", "tdd", "regression testing", "functional testing", "manual testing",
    "test automation", "automation framework", "test planning", "test strategy",
    "mobile testing", "accessibility", "security testing", "cross-browser",
    "llm", "rag", "prompt", "ai agents", "evaluation", "deepeval", "promptfoo", "langchain",
    "computer vision", "machine learning", "voice", "chatbot",
]


def load_resume_data(path):
    """Read the literal resume fields without executing personal Python code."""
    values = {}
    for node in ast.parse(Path(path).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in FIELDS:
            values[node.targets[0].id] = ast.literal_eval(node.value)
    missing = [f for f in FIELDS if f not in values]
    if missing:
        raise ValueError(f"resume_data.py lacks {', '.join(missing)}")
    return values


def _has(term, text):
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None


def _words(text):
    return set(re.findall(r"[a-z][a-z0-9+#./-]{1,}", (text or "").lower()))


def resume_text(data):
    parts = [data["TAGLINE"], data["SUMMARY"], *[f"{a} {b}" for a, b in data["SKILLS"]],
             *[b for job in data["EXPERIENCE"] for b in job["bullets"]],
             *[f"{job['title']} {job['company']}" for job in data["EXPERIENCE"]]]
    return " ".join(parts)


def resume_terms(data):
    """Individual skills as written in the resume's skill groups."""
    terms = []
    for _label, items in data["SKILLS"]:
        for item in re.split(r",|;", items):
            item = re.sub(r"\(.*?\)", "", item).strip().lower()
            if 2 <= len(item) <= 40:
                terms.append(item)
    return terms


def ats_match(jd, data):
    jd_l, text = (jd or "").lower(), resume_text(data).lower()
    vocab = dict.fromkeys([*LEXICON, *resume_terms(data)])
    wanted = [t for t in vocab if _has(t, jd_l)]
    # "selenium webdriver" and "selenium" in one posting are one requirement.
    wanted = [t for t in wanted if not any(t != o and _has(t, o) for o in wanted)]
    wanted.sort(key=lambda t: -len(re.findall(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", jd_l)))
    matched = [t for t in wanted if _has(t, text)]
    missing = [t for t in wanted if not _has(t, text)]
    score = round(100 * len(matched) / len(wanted)) if wanted else 0
    original = resume_text(data)

    def as_written(term):
        found = re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", original, re.I)
        return found.group(0) if found else term
    return {"score": score, "matched": [as_written(t) for t in matched], "missing": missing}


def headline_for(title, tagline):
    t = (title or "").lower()
    bits = []
    if re.search(r"sdet|engineer in test|automation", t):
        bits.append("Test Automation")
    if re.search(r"\bai\b|llm|genai|generative|agent|rag|eval", t):
        bits.append("AI and LLM Application Testing")
    if re.search(r"\bapi\b", t):
        bits.append("API Testing")
    if re.search(r"performance|load", t):
        bits.append("Performance Testing")
    return ("QA Engineer | " + " | ".join(dict.fromkeys(bits))) if bits else tagline


def tailor(data, jd, title):
    jd_words, jd_l = _words(jd), (jd or "").lower()
    stop = {"and", "the", "for", "with", "from", "that", "this", "using", "into", "over", "more", "than",
            "each", "your", "our", "are", "was", "were", "has", "have", "had", "will", "can", "all", "any"}

    def overlap(text):
        return len({w for w in _words(text) if w in jd_words and w not in stop})
    skills = []
    for i, (label, items) in sorted(enumerate(data["SKILLS"]), key=lambda p: (-overlap(f"{p[1][0]} {p[1][1]}"), p[0])):
        parts = [p.strip() for p in items.split(",")]
        parts.sort(key=lambda p: not _has(re.sub(r"\(.*?\)", "", p).strip().lower(), jd_l))
        skills.append((label, ", ".join(parts)))
    experience = [{**job, "bullets": sorted(job["bullets"], key=lambda b: (-overlap(b), job["bullets"].index(b)))}
                  for job in data["EXPERIENCE"]]
    ats = ats_match(jd, data)
    summary = data["SUMMARY"]
    if ats["matched"]:
        summary += " Key skills for this role: " + ", ".join(ats["matched"][:8]) + "."
    return {**data, "TAGLINE": headline_for(title, data["TAGLINE"]), "SKILLS": skills,
            "EXPERIENCE": experience, "SUMMARY": summary, "ats": ats}


def _latin(text):
    text = (text or "").replace("—", "-").replace("–", "-").replace("•", "-")
    text = text.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    return text.encode("latin-1", "replace").decode("latin-1")


def render_pdf(t, path):
    from fpdf import FPDF
    pdf = FPDF(format="A4")
    # A fixed creation date keeps the file byte-identical for the same content,
    # so an unchanged resume does not rewrite an existing Gmail draft.
    pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=timezone.utc))
    pdf.set_title(_latin(f"{t['NAME']} - Resume"))
    pdf.set_author(_latin(t["NAME"]))
    pdf.set_margins(16, 12, 16)
    pdf.set_auto_page_break(True, 12)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def line(text, size=10, style="", align="L", height=5):
        pdf.set_font("Helvetica", style, size)
        pdf.multi_cell(width, height, _latin(text), align=align, new_x="LMARGIN", new_y="NEXT")

    def heading(text):
        pdf.ln(2)
        line(text, 11.5, "B", height=6)
        y = pdf.get_y()
        pdf.line(pdf.l_margin, y, pdf.l_margin + width, y)
        pdf.ln(1.5)

    def bullet(text):
        pdf.set_font("Helvetica", "", 10)
        pdf.set_x(pdf.l_margin + 3)
        pdf.multi_cell(width - 3, 5, _latin("- " + text), new_x="LMARGIN", new_y="NEXT")

    line(t["NAME"], 17, "B", "C", 8)
    line(t["TAGLINE"], 10.5, "", "C", 5.5)
    for contact in t["CONTACT"]:
        line(contact, 9.5, "", "C", 4.8)
    heading("PROFESSIONAL SUMMARY")
    line(t["SUMMARY"])
    heading("SKILLS")
    for label, items in t["SKILLS"]:
        pdf.set_font("Helvetica", "", 10)
        pdf.set_x(pdf.l_margin + 3)
        pdf.multi_cell(width - 3, 5, _latin(f"- **{label}:** {items}"), markdown=True, new_x="LMARGIN", new_y="NEXT")
    heading("PROFESSIONAL EXPERIENCE")
    for job in t["EXPERIENCE"]:
        pdf.ln(1)
        line(f"{job['title']} | {job['company']}", 10.5, "B")
        line(job["dates"], 9.5)
        for b in job["bullets"]:
            bullet(b)
    heading("EDUCATION")
    for degree, detail in t["EDUCATION"]:
        line(degree, 10, "B")
        line(detail, 9.5)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


def build_for_row(data, row, out_dir):
    """Write out_dir/resumes/<job>/<Name>_Resume.pdf for one posting and return
    the report fields describing it."""
    t = tailor(data, row.get("JD Text", ""), row.get("Job Title", ""))
    slug = re.sub(r"[^a-z0-9]+", "-", f"{row.get('Company', '')}-{row.get('Job Title', '')}".lower()).strip("-")[:60]
    slug += "-" + hashlib.sha256(row.get("Job Link", "").encode()).hexdigest()[:8]
    name = re.sub(r"[^A-Za-z0-9]+", "_", data["NAME"].title()).strip("_") + "_Resume.pdf"
    path = Path(out_dir) / "resumes" / slug / name
    render_pdf(t, path)
    ats = t["ats"]
    return {"Resume File": path.relative_to(out_dir).as_posix(), "ATS Match": f"{ats['score']}%",
            "ATS Matched Keywords": ", ".join(ats["matched"]), "ATS Missing Keywords": ", ".join(ats["missing"]),
            "Resume Headline": t["TAGLINE"]}
