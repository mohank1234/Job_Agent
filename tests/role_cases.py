"""Representative role-classification cases.

Run with:  python run.py classify-test

These are the regression tests for the matching engine. Each case is a real-
shaped posting: a title plus enough description for the evidence pass to have
something to work with. `expect` is the role family; `min_base` / `max_base`
bound the deterministic score so a change that quietly starts over-ranking
generic SWE postings fails loudly.
"""

from __future__ import annotations

from jobagent.models import Job
from jobagent.roles import classify

QA_DESC = (
    "You will design test cases, own regression testing and test strategy, "
    "run functional testing, do defect triage and bug reporting, work in an "
    "Agile/Scrum team, and support UAT. REST API testing with Postman and SQL "
    "for backend verification. 4+ years of experience required."
)
AUTO_DESC = (
    "Build and maintain the test automation framework using Selenium WebDriver "
    "and Playwright with Java and TestNG. Own the regression suite, integrate "
    "e2e testing into CI/CD with Jenkins and GitHub Actions, and extend API "
    "automation with RestAssured. 5+ years of experience."
)
AI_QA_DESC = (
    "Own quality for our LLM-powered product. Build evals and benchmark suites, "
    "run accuracy testing and hallucination detection, validate citations and "
    "groundedness, design golden datasets, and use LLM-as-judge scoring. "
    "Experience with RAGAS, DeepEval or promptfoo and Python. 4+ years."
)
RAG_DESC = (
    "Evaluate our retrieval augmented generation pipeline end to end: chunking, "
    "embeddings, vector search and reranking quality. Build regression tests for "
    "retrieval accuracy and groundedness with Python. Test case design and "
    "defect triage experience needed."
)
AGENT_DESC = (
    "Test and evaluate agentic AI workflows: tool calling, multi agent handoffs, "
    "and AI copilot behaviour. Build eval harnesses and benchmark suites in "
    "Python, run regression testing on agent trajectories, and report defects."
)
CHATBOT_DESC = (
    "Quality assurance for our conversational AI assistant. Test intent "
    "recognition and dialogue flows, run regression testing across releases, "
    "validate NLU responses and escalation paths, and file defects. Test case "
    "design and API testing experience required."
)
CV_DESC = (
    "Validate computer vision models in production: object detection and image "
    "classification accuracy, annotation quality, and edge-case failure "
    "analysis. Build automated regression testing with Python and pytest."
)
SDET_DESC = (
    "As an SDET you will write test automation in Java and Python, own the "
    "e2e testing framework, build API automation, and run the regression suite "
    "in CI/CD. 5-7 years of experience in test automation."
)
SWE_TEST_DESC = (
    "Join Developer Productivity to build test infrastructure: the e2e testing "
    "platform, flaky-test detection, CI/CD pipelines and the automation "
    "framework used by every product team. You will own test tooling, coverage "
    "reporting and regression suite performance. Python and Java."
)
SWE_AI_DESC = (
    "Build LLM-powered product features: RAG retrieval, prompt engineering, and "
    "agent tool calling. You will also write evals and benchmark model quality "
    "for the features you ship. Python required."
)
SWE_GENERIC_DESC = (
    "Build and scale our customer-facing web application with React, Node.js "
    "and TypeScript. Own microservices on Kubernetes and AWS, design "
    "distributed systems, and improve latency. 5+ years of experience."
)
DATA_SCI_DESC = (
    "Build predictive models with PyTorch and TensorFlow, run experiments, do "
    "feature engineering on Spark, and present findings. PhD preferred. Deep "
    "learning research experience required."
)
DATA_ENG_DESC = (
    "Own our data pipeline engineering on Spark and Airflow. Build ETL into the "
    "warehouse, manage Kafka streams, and model data for analytics."
)
DEVOPS_DESC = (
    "Own Kubernetes, Terraform and AWS infrastructure. Run the CI/CD platform, "
    "manage observability, and drive incident response and on-call."
)
CLOUD_DESC = (
    "Design cloud architecture on AWS and Azure. Own landing zones, networking, "
    "IAM and Terraform modules for platform teams."
)
MANAGER_DESC = (
    "Lead and grow a team of 12 QA engineers across three product lines. Own "
    "headcount planning, performance reviews, budget and the quality roadmap. "
    "12+ years of experience including 5 years managing managers."
)
NON_SW_QA_DESC = (
    "Perform incoming quality control inspection on manufactured components. "
    "ISO 9001 audits, supplier quality management, calibration records."
)
HARDWARE_DESC = (
    "Own test coverage for our custom accelerator hardware. Build test plans "
    "for silicon bring-up, run electrical validation on the PCB, and drive "
    "failure analysis with the manufacturing team at our contract assembler."
)
USER_RESEARCH_DESC = (
    "Run qualitative studies with users of our AI features. Design research "
    "plans, run interviews and usability sessions, synthesise findings on model "
    "evaluations and quality perception, and present to product teams."
)
ML_ENG_DESC = (
    "Train and fine-tune models with PyTorch. Own the feature store and data "
    "pipeline, run offline evaluation of model quality, and ship to production "
    "inference. Deep learning research background preferred."
)

# (title, description, expected family, min_base, max_base, must_be_candidate)
CASES: list[tuple[str, str, str, int, int, bool]] = [
    # ---- Tier 1: core QA -------------------------------------------------
    ("Senior QA Engineer",                    QA_DESC,   "core_qa",         60, 100, True),
    ("QA Engineer",                           QA_DESC,   "core_qa",         60, 100, True),
    ("Senior Software Quality Engineer",      QA_DESC,   "core_qa",         60, 100, True),
    ("Quality Assurance Engineer",            QA_DESC,   "core_qa",         60, 100, True),
    ("Software Test Engineer",                QA_DESC,   "core_qa",         60, 100, True),
    ("Test Analyst",                          QA_DESC,   "core_qa",         55, 100, True),
    ("QA Analyst",                            QA_DESC,   "core_qa",         55, 100, True),
    # ---- Tier 1: automation / SDET ---------------------------------------
    ("SDET",                                  SDET_DESC, "automation_sdet", 60, 100, True),
    ("Senior SDET",                           SDET_DESC, "automation_sdet", 60, 100, True),
    ("Software Development Engineer in Test", SDET_DESC, "automation_sdet", 60, 100, True),
    ("QA Automation Engineer",                AUTO_DESC, "automation_sdet", 65, 100, True),
    ("Test Automation Engineer",              AUTO_DESC, "automation_sdet", 65, 100, True),
    ("Automation Test Engineer",              AUTO_DESC, "automation_sdet", 65, 100, True),
    ("Senior QA Engineer - Automation",       AUTO_DESC, "automation_sdet", 65, 100, True),
    # ---- Tier 1: AI / LLM QA ---------------------------------------------
    ("AI QA Engineer",                        AI_QA_DESC, "ai_llm_qa",      70, 100, True),
    ("AI Test Engineer",                      AI_QA_DESC, "ai_llm_qa",      70, 100, True),
    ("LLM QA Engineer",                       AI_QA_DESC, "ai_llm_qa",      70, 100, True),
    ("GenAI Test Engineer",                   AI_QA_DESC, "ai_llm_qa",      70, 100, True),
    ("AI/ML Test Engineer",                   AI_QA_DESC, "ai_llm_qa",      70, 100, True),
    ("AI Model Validation Engineer",          AI_QA_DESC, "ai_llm_qa",      65, 100, True),
    ("LLM Evaluation Engineer",               AI_QA_DESC, "ai_evaluation",  65, 100, True),
    ("Model Evaluation Engineer",             AI_QA_DESC, "ai_evaluation",  65, 100, True),
    ("Generative AI Evaluation Engineer",     AI_QA_DESC, "ai_evaluation",  65, 100, True),
    # ---- Tier 2: RAG / agents / chatbot / CV ------------------------------
    ("RAG Evaluation Engineer",               RAG_DESC,     "rag_agent_qa",         60, 100, True),
    ("AI Agent QA Engineer",                  AGENT_DESC,   "rag_agent_qa",         60, 100, True),
    ("Agentic AI Test Engineer",              AGENT_DESC,   "rag_agent_qa",         60, 100, True),
    ("AI Copilot Test Engineer",              AGENT_DESC,   "rag_agent_qa",         60, 100, True),
    ("Chatbot QA Engineer",                   CHATBOT_DESC, "conversational_ai_qa", 60, 100, True),
    ("Conversational AI Test Engineer",       CHATBOT_DESC, "conversational_ai_qa", 60, 100, True),
    ("Computer Vision QA Engineer",           CV_DESC,      "computer_vision_qa",   55, 100, True),
    ("ML Model Test Engineer",                CV_DESC,      "computer_vision_qa",   50, 100, True),
    # ---- Relevant SWE -----------------------------------------------------
    ("Software Engineer in Test",             SDET_DESC,     "automation_sdet", 60, 100, True),
    ("Software Engineer, Developer Productivity", SWE_TEST_DESC, "swe_quality", 55, 100, True),
    ("Software Engineer - AI Applications",   SWE_AI_DESC,   "swe_ai",          35,  79, True),
    # ---- False positives: must NOT outrank real QA work -------------------
    ("Software Engineer",                     SWE_GENERIC_DESC, "off_domain", 0, 45, False),
    ("Senior Software Engineer, Backend",     SWE_GENERIC_DESC, "off_domain", 0, 45, False),
    ("Data Scientist",                        DATA_SCI_DESC,    "off_domain", 0, 45, False),
    ("Senior Data Engineer",                  DATA_ENG_DESC,    "off_domain", 0, 45, False),
    ("DevOps Engineer",                       DEVOPS_DESC,      "off_domain", 0, 45, False),
    ("Cloud Engineer",                        CLOUD_DESC,       "off_domain", 0, 45, False),
    # ---- Seniority / domain guards ---------------------------------------
    ("QA Manager",                            MANAGER_DESC,   "core_qa",    0, 55, True),
    ("Director of Quality Engineering",       MANAGER_DESC,   "core_qa",    0, 55, True),
    ("QA Intern",                             QA_DESC,        "excluded",   0, 30, False),
    ("Quality Control Inspector",             NON_SW_QA_DESC, "off_domain", 0, 40, False),
    # ---- Hardware/manufacturing QA is a different profession ---------------
    ("Manufacturing Test Engineer",           HARDWARE_DESC, "off_domain", 0, 45, False),
    ("Design Verification Engineer",          HARDWARE_DESC, "off_domain", 0, 45, False),
    ("Quality Assurance Technician",          NON_SW_QA_DESC, "off_domain", 0, 45, False),
    ("Contact Center Quality Analyst",         NON_SW_QA_DESC, "off_domain", 0, 45, False),
    ("Call Center Quality Assurance Specialist", NON_SW_QA_DESC, "off_domain", 0, 45, False),
    # ---- AI-adjacent titles that are not engineering QA roles --------------
    ("User Researcher, AI Evaluations",       USER_RESEARCH_DESC, "off_domain", 0, 45, False),
    ("Senior Machine Learning Engineer, Evaluation", ML_ENG_DESC, "off_domain", 0, 45, False),
]


# Geography: the SAME strong QA role in different places. The search is global,
# so location must only shade the score — it must never change the role family
# and never drop the job out of the candidate set.
# (location, workplace, tier) — tier 0 = in target market, 1 = outside it.
# Remote-eligibility: every one of these was marked "remote" by its board, but
# only some can actually be taken from India. Real strings from jobs.db.
# (location, workplace, expected verdict)
REMOTE_CASES: list[tuple[str, str, str]] = [
    ("Remote - Worldwide",       "remote", "open"),
    ("Anywhere",                 "remote", "open"),
    ("India",                    "remote", "open"),
    ("APAC",                     "remote", "open"),
    ("Remote - India",           "remote", "open"),
    # These read as "remote" but are tied to one country.
    ("Canada",                   "remote", "region_locked"),
    ("San Francisco",            "remote", "region_locked"),
    ("USA | Remote",             "remote", "region_locked"),
    ("Brazil - Remote",          "remote", "region_locked"),
    ("Egypt",                    "remote", "region_locked"),
    ("Sri Lanka",                "remote", "region_locked"),
    ("Mexico",                   "remote", "region_locked"),
    ("United States",            "remote", "region_locked"),
    ("SF Office",                "remote", "region_locked"),
    # Bare "Remote" with no country stated anywhere.
    ("Remote",                   "remote", "unspecified"),
]


def run_remote_cases(profile, cases=None) -> list[dict]:
    from jobagent.roles import remote_eligibility
    results = []
    for location, workplace, expected in (cases or REMOTE_CASES):
        job = Job(source="test", company="TestCo", title="QA Engineer",
                  url="https://example.com/1", location=location,
                  workplace=workplace, description=QA_DESC)
        verdict, _note = remote_eligibility(job)
        results.append({
            "title": f"remote: {location!r}",
            "expected": expected,
            "got": verdict,
            "base_score": 0,
            "candidate": verdict == "open",
            "ok": verdict == expected,
        })
    return results


# QA-only strictness: developer roles must NOT become candidates, however much
# testing their description mentions.
QA_ONLY_CASES: list[tuple[str, str, bool]] = [
    ("Senior QA Engineer",                     QA_DESC,       True),
    ("SDET",                                   SDET_DESC,     True),
    ("Software Engineer in Test",              SDET_DESC,     True),
    ("AI QA Engineer",                         AI_QA_DESC,    True),
    ("LLM Evaluation Engineer",                AI_QA_DESC,    True),
    # Developer roles — excluded even with heavy testing language.
    ("Software Engineer, Generative AI",       SWE_AI_DESC,   False),
    ("Senior Software Engineer, Agents",       SWE_AI_DESC,   False),
    ("Software Engineer, AI Platform",         SWE_AI_DESC,   False),
    ("Senior Full Stack Engineer (Java/React)", SWE_GENERIC_DESC, False),
    ("Software Engineer, Developer Productivity", SWE_TEST_DESC, False),
    ("Associate Application Engineer",         SWE_GENERIC_DESC, False),
]


def run_qa_only_cases(profile, cases=None) -> list[dict]:
    from jobagent.roles import candidates as pick
    results = []
    for title, desc, should_qualify in (cases or QA_ONLY_CASES):
        job = Job(source="test", company="TestCo", title=title,
                  url="https://example.com/1", location="Remote - Worldwide",
                  workplace="remote", description=desc)
        job.apply_classification(classify(job, profile))
        qualifies = bool(pick([job], qa_only=True))
        results.append({
            "title": f"qa-only: {title[:34]}",
            "expected": "candidate" if should_qualify else "excluded",
            "got": ("candidate" if qualifies else "excluded") + f" [{job.role_family}]",
            "base_score": job.base_score,
            "candidate": qualifies,
            "ok": qualifies == should_qualify,
        })
    return results


# Employer quality: identical role and city, only the company changes.
# (company, expect_preferred, expect_staffing)
COMPANY_CASES: list[tuple[str, bool, bool]] = [
    ("Okta",                                    True,  False),
    ("Databricks",                              True,  False),
    ("Paytm",                                   True,  False),
    ("Glean",                                   True,  False),
    ("Rubrik",                                  True,  False),
    ("Atlassian India",                         True,  False),
    ("Aaksar Consulting Solutions Chennai",     False, True),
    ("Allegis Services India Pvt. Ltd.",        False, True),
    ("Nityo Infotech",                          False, True),
    ("Sketchhr Manpower And Hr Consultancy",    False, True),
    ("Randstad India",                          False, True),
    ("ABC Recruitment Services",                False, True),
    ("Some Unknown Pvt Ltd",                    False, False),
]


def run_company_cases(profile, cases=None) -> list[dict]:
    """A product company must outscore a staffing firm on the same posting."""
    results = []
    baseline = None
    for company, want_preferred, want_staffing in (cases or COMPANY_CASES):
        job = Job(source="test", company=company,
                  title="Senior QA Automation Engineer", url="u",
                  location="Hyderabad, India", workplace="onsite",
                  description=AUTO_DESC)
        c = classify(job, profile)
        is_staffing = any("staffing" in n for n in c.notes)
        if company == "Some Unknown Pvt Ltd":
            baseline = c.base_score
        ok = is_staffing == want_staffing
        if want_preferred:
            ok = ok and c.base_score >= 95        # bonus lifts it to the top
        if want_staffing:
            ok = ok and c.base_score <= 80        # penalty pushes it down
        results.append({
            "title": f"employer: {company[:34]}",
            "expected": ("preferred" if want_preferred else
                         "staffing" if want_staffing else "neutral"),
            "got": ("staffing" if is_staffing else "neutral/preferred"),
            "base_score": c.base_score,
            "candidate": c.candidate,
            "ok": ok,
        })
    return results


GEO_CASES: list[tuple[str, str, int]] = [
    # IN SCOPE: Hyderabad onsite/hybrid, and remote open to anywhere.
    ("Hyderabad, India",          "onsite", 0),
    ("Hyderabad, Telangana, India", "hybrid", 0),
    ("Remote - Worldwide",        "remote", 0),
    ("Anywhere",                  "remote", 0),
    ("India",                     "remote", 0),
    # OUT OF SCOPE: onsite/hybrid anywhere that is not Hyderabad.
    ("Bengaluru, India",          "hybrid", 1),
    ("Pune, India",               "onsite", 1),
    ("Riyadh, Saudi Arabia",      "onsite", 1),
    ("London, UK",                "onsite", 1),
    ("Austin, TX, United States", "onsite", 1),
    # OUT OF SCOPE: "remote" that is really remote-within-one-country.
    ("Canada",                    "remote", 1),
    ("San Francisco",             "remote", 1),
    ("USA | Remote",              "remote", 1),
    ("Brazil - Remote",           "remote", 1),
]


# --- Singapore / Employment Pass -------------------------------------------
# Onsite Singapore is in scope only when the employer can sponsor an EP. The
# salary line is written by the MyCareersFuture source; the rest is language
# that turns up verbatim in real Singapore postings.
SG_SPONSOR_DESC = AUTO_DESC + (
    "\n\n[work authorisation: monthly salary S$7,000-9,500 meets the Employment "
    "Pass floor of S$5,600. An EP is legally possible for this role.]"
)
SG_BELOW_DESC = AUTO_DESC + (
    "\n\n[work authorisation: monthly salary S$3,800-4,500 below the Employment "
    "Pass floor of S$5,600. No employer can sponsor an EP below the floor.]"
)
SG_LOCALS_DESC = AUTO_DESC + (
    " Only Singaporeans and PR need apply. We are not able to sponsor "
    "employment passes for this position."
)
SG_WORKPASS_DESC = AUTO_DESC + (
    " Applicants must already hold a valid work pass in Singapore."
)
SG_EXPLICIT_DESC = AUTO_DESC + (
    " Visa sponsorship is available for the right candidate, and we provide "
    "relocation support."
)
SG_INDUSTRIAL_DESC = (
    "Perform QA/QC inspection on the production line, carry out incoming "
    "inspection of raw materials, maintain ISO 9001 documentation, raise "
    "non-conformance reports and drive corrective and preventive action with "
    "the shipyard team. Marine and offshore fabrication experience required."
)

# (title, description, workplace, expected singapore_eligibility verdict,
#  should it survive the QA-only + location scope cap)
SG_CASES: list[tuple[str, str, str, str, bool]] = [
    ("Senior Test Automation Engineer", SG_SPONSOR_DESC,  "onsite", "sponsors",    True),
    ("QA Automation Engineer",          SG_EXPLICIT_DESC, "onsite", "sponsors",    True),
    ("SDET",                            SG_BELOW_DESC,    "onsite", "below_floor", False),
    ("QA Engineer",                     SG_LOCALS_DESC,   "onsite", "locals_only", False),
    ("Test Automation Engineer",        SG_WORKPASS_DESC, "onsite", "locals_only", False),
    # Silent on sponsorship: still worth surfacing, one email settles it.
    ("Senior QA Engineer",              AUTO_DESC,        "onsite", "unspecified", True),
    # Shop-floor quality work advertised as a plain QA title must not survive.
    ("QA Engineer",                     SG_INDUSTRIAL_DESC, "onsite", "unspecified", False),
]


def run_singapore_cases(profile, cases=None) -> list[dict]:
    """Singapore is the one place abroad where onsite is in scope, and only
    with sponsorship. Checks the verdict and whether the scope cap agrees."""
    # classify_all, not classify: only the former stamps role_family onto the
    # job, and scope_cap reads that to apply the QA-only rule.
    from jobagent.roles import singapore_eligibility, scope_cap, classify_all

    results = []
    for title, desc, workplace, want_verdict, want_in_scope in (cases or SG_CASES):
        job = Job(
            source="test", company="TestCo Pte Ltd", title=title,
            url="https://example.com/sg", location="Singapore",
            workplace=workplace, description=desc,
        )
        classify_all([job], profile)
        verdict, _ = singapore_eligibility(job, profile)
        cap, _ = scope_cap(job, profile, qa_only=True)
        in_scope = cap == 100
        ok = verdict == want_verdict and in_scope == want_in_scope
        results.append({
            "title": f"SG {title[:30]}",
            "expected": f"{want_verdict}/{'in' if want_in_scope else 'out'}",
            "got": f"{verdict}/{'in' if in_scope else 'out'}",
            "base_score": job.base_score,
            "candidate": in_scope,
            "ok": ok,
        })
    return results


def run_geo_cases(profile, cases=None) -> list[dict]:
    """Asserts the relationship, not magic numbers: every in-market location
    outscores every out-of-market one, and none of them is ever rejected."""
    cases = cases or GEO_CASES
    scored = []
    for location, workplace, tier in cases:
        job = Job(
            source="test", company="TestCo",
            title="Senior QA Automation Engineer",
            url="https://example.com/1", location=location,
            workplace=workplace, description=AUTO_DESC,
        )
        scored.append((location, workplace, tier, classify(job, profile)))

    worst_in_market = min(
        (c.base_score for _l, _w, t, c in scored if t == 0), default=0
    )
    results = []
    for location, workplace, tier, c in scored:
        ok = c.family == "automation_sdet" and c.candidate
        if tier == 0:
            ok = ok and c.base_score >= 60
            expected = "in-market, >=60"
        else:
            # Penalised, but still a candidate — location never rejects.
            ok = ok and c.base_score < worst_in_market
            expected = f"scores < {worst_in_market}"
        results.append({
            "title": f"{location} ({workplace})",
            "expected": expected,
            "got": c.family,
            "base_score": c.base_score,
            "candidate": c.candidate,
            "ok": ok,
        })
    return results


def run_cases(profile, cases=None) -> list[dict]:
    results = []
    for title, desc, expect, min_base, max_base, want_candidate in (cases or CASES):
        # Hyderabad onsite: an in-scope location, so these cases test the
        # ROLE classification rather than the location policy. Location rules
        # have their own cases in GEO_CASES / REMOTE_CASES.
        job = Job(
            source="test", company="TestCo", title=title,
            url="https://example.com/1", location="Hyderabad, India",
            workplace="onsite", description=desc,
        )
        c = classify(job, profile)
        ok = (
            c.family == expect
            and min_base <= c.base_score <= max_base
            and c.candidate == want_candidate
        )
        results.append({
            "title": title,
            "expected": expect,
            "got": c.family,
            "base_score": c.base_score,
            "candidate": c.candidate,
            "tier": c.tier,
            "seniority": c.seniority,
            "ok": ok,
            "bounds": (min_base, max_base),
            "want_candidate": want_candidate,
        })
    return results
