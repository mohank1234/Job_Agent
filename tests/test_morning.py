from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from jobagent import morning
from jobagent.outreach.daily_research import cached_draft, grounded_draft, validate_research
from jobagent.profile import load_profile
from jobagent.runtime import process_lock
from tests.test_regressions import posting


@pytest.mark.parametrize('hour,minute,allowed', [(5,59,False),(6,0,True),(10,59,True),(11,0,False),(23,0,False)])
def test_ist_window(hour, minute, allowed):
    now = datetime(2026, 9, 9, hour, minute, tzinfo=morning.IST)
    assert morning.in_window(now.astimezone(timezone.utc)) is allowed


def test_ist_date_rollover():
    assert morning.day_key(datetime(2026,9,8,20,tzinfo=timezone.utc)) == '2026-09-09'


@pytest.mark.parametrize('text,result', [
    ('Qualifications: 4-6 years of QA experience.', '4-6 year minimum'),
    ('Requirements: 5+ years software testing experience.', '4-6 year minimum'),
    ('Requirements: 6–10 years of professional experience.', '4-6 year minimum'),
    ('Requirements: 7+ years QA experience.', 'Outside 4-6 year focus'),
    ('Requirements: 3-5 years QA experience.', 'Outside 4-6 year focus'),
    ('Requirements: 8 years QA experience; 4 years Python experience.', 'Outside 4-6 year focus'),
    ('We have 5 years of funding. Requirements: Python and API testing.', 'Not stated clearly'),
    ('Requirements: Experience with Python and test automation.', 'Not stated clearly'),
    ('Requirements: 4 years Python experience.', 'Not stated clearly'),
])
def test_experience_minimum(text, result):
    assert morning.experience_focus(text)[0] == result


def test_outside_window_performs_no_preflight_or_work(tmp_path):
    def forbidden(*a, **kw):
        raise AssertionError('Outside-window action attempted')
    result = morning.run_daily({}, None, tmp_path/'out', state_dir=tmp_path/'state',
                               now=datetime(2026,9,9,11,tzinfo=morning.IST), work=forbidden,
                               preflight=forbidden, publish=forbidden)
    assert result['status'] == 'skipped_outside_window'
    assert not (tmp_path/'state').exists()


def test_once_daily_and_publish_resume(tmp_path):
    calls = []
    def work(*args, **kwargs):
        calls.append('work')
        return {'drafts': 1}
    def publish(out):
        calls.append('publish')
        if calls.count('publish') == 1:
            raise ConnectionError('temporary outage')
        return {'verified': True}
    kwargs = dict(initial=True, state_dir=tmp_path/'state', work=work, publish=publish,
                  now=datetime(2026,9,9,16,tzinfo=morning.IST))
    with pytest.raises(ConnectionError):
        morning.run_daily({}, None, tmp_path/'out', **kwargs)
    assert morning.run_daily({}, None, tmp_path/'out', **kwargs)['status'] == 'completed'
    assert morning.run_daily({}, None, tmp_path/'out', **kwargs)['status'] == 'skipped_already_completed'
    assert calls == ['work', 'publish', 'publish']
    kwargs['now'] = datetime(2026,9,10,6,tzinfo=morning.IST)
    assert morning.run_daily({}, None, tmp_path/'out', **kwargs)['status'] == 'completed'
    assert calls.count('work') == 2


def test_concurrent_run_cannot_enter(tmp_path):
    state = tmp_path/'state'
    with process_lock(state/'daily.lock'):
        with pytest.raises(RuntimeError, match='workflow lock'):
            morning.run_daily({}, None, tmp_path, initial=True, state_dir=state,
                              work=lambda *a, **k: pytest.fail('Duplicate entered'))


def test_source_coverage_keeps_failed_boards_and_resumes(tmp_path):
    calls = []
    def fetch(url, client):
        calls.append(url)
        if url.endswith('/bad'):
            raise httpx.HTTPStatusError('unavailable', request=httpx.Request('GET',url), response=httpx.Response(404))
        return [posting()], 'https://api.ashbyhq.com/posting-api/job-board/example'
    directory = morning.board_directory({'ashby':['example','bad','example'], 'unsupported':['foo']})
    result = morning.collect_boards(directory, tmp_path, fetch=fetch)
    assert len(result) == 2
    assert {r['Status'] for r in result} == {'Fetched','HTTP 404'}
    assert sum(r['Postings'] for r in result) == 1
    morning.collect_boards(directory, tmp_path, fetch=fetch)
    assert len(calls) == 2


def test_compensation_not_in_prompt():
    p = load_profile(Path(__file__).resolve().parents[1]/'profile.yaml.example')
    p.raw['preferences'].update(compensation_filter_enabled=False, min_ctc_lpa=9999)
    assert '9999' not in p.llm_block()
    assert 'Do not filter, penalize or rank jobs by pay' in p.llm_block()


def test_us_remote_benefit_does_not_establish_india_eligibility():
    from jobagent.runtime import now_iso
    p = load_profile(Path(__file__).resolve().parents[1]/'profile.yaml.example')
    p.raw['identity']['experience']['years'] = 5
    job = posting()
    job.location = 'Remote US'
    job.description += ' Qualifications: 5 years QA experience. Benefits: work from anywhere.'
    record = {'Company':'example','Vendor':'ashby','Status':'Fetched','Checked At':now_iso(),
              'Source URL':'https://api.ashbyhq.com/posting-api/job-board/example','jobs':[morning._job_dict(job)]}
    row = morning.assess_boards([record],p)[0]
    assert row['Fit Status'] == 'out_of_scope'
    assert 'India eligibility not established' in row['Fit Notes']


def source_fixture():
    quote = 'Alex Example, CTO at Example, shares alex@example.com for engineering hiring.'
    url = 'https://example.com/team'
    linkedin = 'https://www.linkedin.com/in/alex-example'
    source = {'url':url, 'snippet':quote+' '+linkedin+' Backed by Index Ventures.', 'evidence_status':'Fetched public company/YC page today'}
    suggested = {'investors':'Index Ventures', 'investment_source':url, 'investment_quote':'Backed by Index Ventures.',
                 'manager_name':'Alex Example','manager_role':'CTO','manager_source':url,'manager_quote':quote,
                 'manager_linkedin':linkedin,'public_work_email':'alex@example.com','email_source':url,'email_quote':quote}
    return source, suggested


def test_contacts_require_real_quotes_urls_and_public_association():
    source, data = source_fixture()
    result = validate_research(data, [source], 'Example')
    assert result['Public Work Email'] == 'alex@example.com'
    assert result['Investor Backing'] == 'Index Ventures'
    assert 'unverified' in result['Email Evidence']
    for bad in ({'public_work_email':'alex.e@example.com'}, {'email_source':'https://madeup.com'},
                {'email_quote':'alex@example.com'}):
        assert not validate_research({**data, **bad}, [source], 'Example').get('Public Work Email')
    assert not validate_research({**data,'manager_linkedin':'https://www.linkedin.com/in/invented'},[source],'Example').get('Manager LinkedIn')
    assert not validate_research({**data,'investors':'Sequoia Capital'}, [source], 'Example').get('Investor Backing')


def test_drafts_grounded_cached_and_approval_pending(tmp_path):
    row = {'Company':'Example','Job Title':'Senior QA Engineer', 'Job Link':'https://jobs.ashbyhq.com/example/1',
           'JD SHA256':'one','JD Text':'Design and maintain Selenium and API regression tests. Qualifications: 5 years QA experience.'}
    facts = {'name':'Candidate', 'phone':'5550101234', 'bullets':['Built Selenium regression tests from scratch.', 'Wrote API tests with Rest Assured.']}
    draft, reused = cached_draft(row, {}, facts, tmp_path)
    assert not reused
    assert 'Selenium automation and API testing' in draft['Cold Email']
    assert draft['Cold Email'].endswith('Thanks,\nCandidate\n5550101234')
    assert draft['Cold Email'].count('5550101234') == 1
    assert '5550101234' not in draft['LinkedIn Note']
    assert len(draft['LinkedIn Note']) <= 300
    assert draft['Approval Status'] == 'Pending user approval; do not send'
    assert cached_draft(row, {}, facts, tmp_path)[1]
    assert not cached_draft({**row,'JD SHA256':'changed'}, {}, facts, tmp_path)[1]
    assert not cached_draft(row, {'Manager Name':'Different Recipient'}, facts, tmp_path)[1]
    assert len(grounded_draft({**row,'Company':'VeryLongCompany'*80}, {}, facts)['LinkedIn Note']) <= 300
    addressed = grounded_draft(row, {'Manager Name':'Alex Leader','Email Contact Name':'Sam Recruiter'}, facts)
    assert addressed['Cold Email'].startswith('Hi Sam,')
    assert addressed['LinkedIn Note'].startswith('Hi Alex,')


def test_daily_work_exports_full_jd_decisions_and_drafts(tmp_path, monkeypatch):
    import csv
    from jobagent import llm
    from jobagent.outreach import daily_research
    from jobagent.outreach import report_drafts
    from jobagent.runtime import now_iso
    project = tmp_path/'project'
    project.mkdir()
    (project/'companies.yaml').write_text('ashby: [example]\n', encoding='utf-8')
    (project/'resume').mkdir()
    (project/'resume'/'resume_data.py').write_text("NAME='Candidate'\nSUMMARY='QA'\nEXPERIENCE=[{'bullets':['Built Selenium tests.', 'Wrote API tests.']}]\n", encoding='utf-8')
    job = posting()
    job.description += ' Qualifications: 5 years of QA experience.'
    record = {'Company':'example','Vendor':'ashby','Board URL':'https://jobs.ashbyhq.com/example',
              'Status':'Fetched','Checked At':now_iso(),'Postings':1,'QA titles':1,
              'Source URL':'https://api.ashbyhq.com/posting-api/job-board/example','jobs':[morning._job_dict(job)]}
    monkeypatch.setattr(morning, 'ROOT', project)
    monkeypatch.setattr(morning, 'collect_boards', lambda *a, **k: [record])
    monkeypatch.setattr(llm, 'make_provider', lambda cfg: None)
    monkeypatch.setattr(daily_research, 'research_company', lambda *a, **k: {'Research Checked At':now_iso()})
    profile = load_profile(Path(__file__).resolve().parents[1]/'profile.yaml.example')
    profile.raw['identity']['experience']['years'] = 5
    out, run_dir = project/'out', project/'state'/'2026-09-09'
    run_dir.mkdir(parents=True)
    phases = []
    def sync_after_report(output, sender, **kwargs):
        assert (output/'JobAgent Report.xlsx').stat().st_size > 1000
        prepared = list(csv.DictReader((output/'Startup Outreach.csv').open(encoding='utf-8-sig')))
        assert prepared[0]['Cold Email'].endswith('Candidate\n5550101234')
        phases.append('gmail_after_report')
        return [{'Status':'Created and read back'}]
    monkeypatch.setattr(report_drafts, 'sync_report_drafts', sync_after_report)
    summary = morning.daily_work({'morning':{'gmail_drafts':True,'signature_phone':'5550101234'}}, profile, out, run_dir, progress=lambda *a:None)
    assert phases == ['gmail_after_report']
    assert summary['workflow_order'] == ['Job search and Excel report','Gmail drafts','Drive publication']
    assert summary['drafts'] == 1 and summary['outreach_sent'] == 0
    assert summary['focus_roles'] == 1
    rows = list(csv.DictReader((out/'Startup Outreach.csv').open(encoding='utf-8-sig')))
    assert rows[0]['JD Text'] == job.description
    assert rows[0]['Approval Status'].startswith('Pending user approval')
    assert len(rows[0]['LinkedIn Note']) <= 300
    assert (out/'All Job Decisions.csv').is_file()
    assert (out/'JobAgent Report.xlsx').stat().st_size > 1000
