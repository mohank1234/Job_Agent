import csv

import pytest

from jobagent.outreach import tailored_resume
from tests.test_morning import RESUME

JD = ("Senior SDET. Build Playwright and Python automation. API testing with Postman. "
      "Cypress and Kubernetes are a plus.")
ROW = {"Company": "Acme", "Job Title": "Senior SDET", "JD Text": JD, "Job Link": "https://jobs.ashbyhq.com/acme/1"}


def test_ats_score_counts_only_what_the_resume_has():
    ats = tailored_resume.ats_match(JD, RESUME)
    assert set(ats["matched"]) >= {"Playwright", "Python", "Postman"}
    assert "cypress" in ats["missing"] and "kubernetes" in ats["missing"]
    assert ats["score"] == round(100 * len(ats["matched"]) / (len(ats["matched"]) + len(ats["missing"])))


def test_tailoring_reorders_but_never_adds():
    t = tailored_resume.tailor(RESUME, JD, "Senior SDET")
    assert t["TAGLINE"].startswith("QA Engineer | Test Automation")
    assert t["SKILLS"][0][1].startswith("Playwright, Python")
    everything = " ".join([t["SUMMARY"], *[i for _, i in t["SKILLS"]], *[b for j in t["EXPERIENCE"] for b in j["bullets"]]])
    assert "Cypress" not in everything and "Kubernetes" not in everything


def test_pdf_is_readable_text_and_byte_stable(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    first = tailored_resume.build_for_row(RESUME, ROW, tmp_path)
    path = tmp_path / first["Resume File"]
    assert path.name == "Candidate_Senior_QA_SDET_5Yrs.pdf" and path.read_bytes().startswith(b"%PDF-")
    text = pypdf.PdfReader(str(path)).pages[0].extract_text()
    assert "PROFESSIONAL EXPERIENCE" in text and "Playwright" in text
    before = path.read_bytes()
    tailored_resume.build_for_row(RESUME, ROW, tmp_path)
    assert path.read_bytes() == before  # an unchanged resume never rewrites a Gmail draft
    assert first["ATS Match"].endswith("%") and "cypress" in first["ATS Missing Keywords"]


def test_each_draft_gets_its_own_tailored_resume(tmp_path):
    from tests.test_report_followup import FakeGmail
    from jobagent.outreach.report_drafts import decode_draft, sync_report_drafts
    fields = ["Company", "Job Link", "Cold Email Subject", "Cold Email", "Public Work Email", "Email Source",
              "Email Evidence", "Resume File"]
    rows = []
    for company in ("acme", "beta"):
        built = tailored_resume.build_for_row(RESUME, {**ROW, "Company": company, "Job Link": f"https://x/{company}"}, tmp_path)
        rows.append({"Company": company, "Job Link": f"https://x/{company}", "Cold Email Subject": f"QA at {company}",
                     "Cold Email": "Hi,\n\nI've attached my resume.", "Public Work Email": f"cto@{company}.io",
                     "Email Source": f"https://{company}.io/team", "Email Evidence": "Verified",
                     "Resume File": built["Resume File"]})
    with (tmp_path / "Startup Outreach.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    service = FakeGmail()
    results = sync_report_drafts(tmp_path, "candidate@example.com", ledger_path=tmp_path / "ledger.json", service=service)
    assert [r["Status"] for r in results] == ["Created and read back"] * 2
    attached = [decode_draft(service.data[k])["attachments"] for k in ("1", "2")]
    assert all(a[0]["filename"] == "Candidate_Senior_QA_SDET_5Yrs.pdf" and a[0]["mime_type"] == "application/pdf" for a in attached)


@pytest.mark.parametrize("title, role", [
    ("Senior SDET", "Senior_QA_SDET"),
    ("SDET", "QA_SDET"),
    ("QA Automation Engineer", "QA_Automation_Engineer"),
    ("Senior QA Engineer", "Senior_QA_Engineer"),
    ("AI QA Engineer", "AI_QA_Engineer"),
    ("LLM Quality Engineer", "AI_QA_Engineer"),
    ("Agentic AI QA", "AI_QA_Engineer"),
    ("QA Engineer", "QA_Engineer"),
    ("Software Engineer in Test", "QA_SDET"),
    ("", "Resume"),
])
def test_professional_role_specific_filename(title, role):
    data = {**RESUME, "NAME": "Krishna Mohan B"}
    filename = tailored_resume.file_name(data, title)
    assert filename == f"Krishna_Mohan_B_{role}_5Yrs.pdf"
    assert "ATS" not in filename.upper()
