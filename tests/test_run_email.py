from tools.send_run_email import build_body


def test_email_leads_with_what_changed():
    summary = {"headline": "3 new jobs, 2 new companies, 1 new contacts; 40 still open from earlier.",
               "whats_new": {"new_jobs": 3}, "counts": {}}
    body = build_body({"date_ist": "2026-09-28", "summary": summary},
                      [{"Company": "gamma", "Job Title": "QA Engineer", "Job Link": "https://jobs.ashbyhq.com/gamma/1"}])
    lines = body.splitlines()
    assert lines[2].startswith("3 new jobs, 2 new companies")
    assert "New today:" in lines and "Nothing new today" not in body


def test_email_says_plainly_when_nothing_is_new():
    summary = {"headline": "0 new jobs, 0 new companies, 0 new contacts; 40 still open from earlier.",
               "whats_new": {"new_jobs": 0}, "counts": {}}
    assert "Nothing new today" in build_body({"date_ist": "2026-09-28", "summary": summary}, [])
