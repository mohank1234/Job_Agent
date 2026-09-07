"""Build an ATS-targeted resume in .docx and .md from one source of truth.

This file is generic rendering logic and contains no personal data — see
`resume_data.py` (gitignored; copy from `resume_data.example.py` on a fresh
checkout) for the actual name, contact info, and work history. Before this
split (repo audit 2026-09-07 finding #12), personal data and this rendering
code lived in the same file, so gitignoring the personal data also
gitignored the code — `apply-kit` could not even be imported on a fresh
checkout. Now only the data file is missing on a fresh checkout, with a
template showing exactly what to fill in.

ATS rules this file obeys:
  - single column, no tables, no text boxes, no images, no headers/footers
  - standard section headings an ATS parser recognises
  - plain bullets, one font, no icons or special glyphs
  - dates in a consistent "Mon YYYY - Mon YYYY" format
  - contact details in the body, never in a header

Run:  python resume/build_resume.py
"""

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

try:
    from resume_data import (CONTACT, EDUCATION, EXPERIENCE, NAME, SKILLS,
                              SUMMARY, TAGLINE)
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "resume/resume_data.py not found. This holds your actual resume "
        "content (name, contact info, work history) and is gitignored, so "
        "a fresh checkout doesn't have one. Create it with:\n\n"
        "    cp resume/resume_data.example.py resume/resume_data.py\n\n"
        "then fill in your own details."
    ) from exc

OUT = Path(__file__).parent


# --------------------------------------------------------------------------- docx

def build_docx(path: Path) -> None:
    doc = Document()

    # One font throughout. ATS parsers choke on decorative fonts.
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    style.paragraph_format.space_after = Pt(2)
    style.paragraph_format.space_before = Pt(0)

    for section in doc.sections:
        section.top_margin = section.bottom_margin = Pt(36)
        section.left_margin = section.right_margin = Pt(45)

    def para(text="", size=10.5, bold=False, align=None, space_before=0, space_after=2):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.space_after = Pt(space_after)
        if align:
            p.alignment = align
        run = p.add_run(text)
        run.bold = bold
        run.font.size = Pt(size)
        run.font.color.rgb = RGBColor(0, 0, 0)
        return p

    def heading(text):
        p = para(text, size=11.5, bold=True, space_before=9, space_after=3)
        return p

    def bullet(text):
        p = doc.add_paragraph(text, style="List Bullet")
        p.paragraph_format.space_after = Pt(2)
        for run in p.runs:
            run.font.size = Pt(10.5)
        return p

    para(NAME, size=17, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    para(TAGLINE, size=11, align=WD_ALIGN_PARAGRAPH.CENTER)
    for line in CONTACT:
        para(line, size=10, align=WD_ALIGN_PARAGRAPH.CENTER)

    heading("PROFESSIONAL SUMMARY")
    para(SUMMARY)

    heading("SKILLS")
    for label, items in SKILLS:
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(2)
        r = p.add_run(f"{label}: ")
        r.bold = True
        r.font.size = Pt(10.5)
        r2 = p.add_run(items)
        r2.font.size = Pt(10.5)

    heading("PROFESSIONAL EXPERIENCE")
    for job in EXPERIENCE:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(1)
        r = p.add_run(f"{job['title']} | {job['company']}")
        r.bold = True
        r.font.size = Pt(11)
        para(job["dates"], size=10)
        for b in job["bullets"]:
            bullet(b)

    heading("EDUCATION")
    for degree, detail in EDUCATION:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(1)
        r = p.add_run(degree)
        r.bold = True
        r.font.size = Pt(10.5)
        para(detail, size=10)

    doc.save(path)


# ----------------------------------------------------------------------- markdown

def build_md(path: Path) -> None:
    lines = [f"# {NAME}", "", f"**{TAGLINE}**", ""]
    lines += [line for line in CONTACT] + [""]
    lines += ["## PROFESSIONAL SUMMARY", "", SUMMARY, ""]
    lines += ["## SKILLS", ""]
    lines += [f"- **{label}:** {items}" for label, items in SKILLS] + [""]
    lines += ["## PROFESSIONAL EXPERIENCE", ""]
    for job in EXPERIENCE:
        lines += [f"### {job['title']} | {job['company']}", "", job["dates"], ""]
        lines += [f"- {b}" for b in job["bullets"]] + [""]
    lines += ["## EDUCATION", ""]
    for degree, detail in EDUCATION:
        lines += [f"**{degree}**", "", detail, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    build_docx(OUT / "Krishna_Mohan_B_Resume_ATS.docx")
    build_md(OUT / "Krishna_Mohan_B_Resume_ATS.md")
    print(f"written to {OUT}")
