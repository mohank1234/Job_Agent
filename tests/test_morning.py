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


def test_gmail_failure_is_published_but_not_marked_completed(tmp_path):
    calls = []
    def work(*args, **kwargs):
        calls.append('work')
        return {'issues':[{'stage':'gmail_drafts','error':'ConnectionError'}] if calls.count('work') == 1 else []}
    def publish(out):
        calls.append('publish')
        return {'verified':True}
    kwargs = dict(initial=True, state_dir=tmp_path/'state', work=work, publish=publish,
                  now=datetime(2026,9,10,16,tzinfo=morning.IST))
    assert morning.run_daily({}, None, tmp_path/'out', **kwargs)['status'] == 'partial'
    assert morning.run_daily({}, None, tmp_path/'out', **kwargs)['status'] == 'completed'
    assert morning.run_daily({}, None, tmp_path/'out', **kwargs)['status'] == 'skipped_already_completed'
    assert calls == ['work','publish','work','publish']


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
    assert draft['Cold Email'].endswith('Thanks & regards,\nCandidate\n5550101234')
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
    from jobagent.enrich import exa
    monkeypatch.setattr(morning, 'ROOT', project)
    monkeypatch.setattr(morning, 'collect_boards', lambda *a, **k: [record])
    monkeypatch.setattr(llm, 'make_provider', lambda cfg: None)
    monkeypatch.setattr(exa, 'exa_search', lambda *a, **k: [])
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


def test_daily_work_moves_on_to_fresh_companies_and_reports_what_is_new(tmp_path, monkeypatch):
    import dataclasses
    import hashlib
    import json
    from jobagent import discovery, llm
    from jobagent.enrich import exa
    from jobagent.outreach import daily_research
    from jobagent.runtime import now_iso
    project = tmp_path/'project'
    (project/'resume').mkdir(parents=True)
    (project/'companies.yaml').write_text('ashby: [alpha, beta]\n', encoding='utf-8')
    (project/'resume'/'resume_data.py').write_text("NAME='Candidate'\nSUMMARY='QA'\nEXPERIENCE=[{'bullets':['Built Selenium tests.']}]\n", encoding='utf-8')
    # alpha already has a draft/sent email; beta has never been contacted.
    alpha_key = hashlib.sha256(b'alpha').hexdigest()[:24]
    (project/'gmail_report_drafts.json').write_text(json.dumps({alpha_key: {'state': 'sent'}}), encoding='utf-8')
    records = []
    for company in ('alpha', 'beta'):
        job = dataclasses.replace(posting(), company=company, url=f'https://jobs.ashbyhq.com/{company}/1')
        job.description += ' Qualifications: 5 years of QA experience.'
        records.append({'Company':company,'Vendor':'ashby','Board URL':f'https://jobs.ashbyhq.com/{company}',
                        'Status':'Fetched','Checked At':now_iso(),'Postings':1,'QA titles':1,
                        'Source URL':f'https://api.ashbyhq.com/posting-api/job-board/{company}','jobs':[morning._job_dict(job)]})
    news = [{'url':'https://news.example/funding','title':'Gamma Robotics raises seed round','snippet':'Gamma Robotics raised $5M.'}]
    searches = []
    def search(query, *a, **k):
        searches.append((query, k.get('include_domains')))
        return news if 'include_domains' not in k else []
    class Provider:
        def structured(self, system, prompt, model):
            return model(companies=[{'name':'Gamma Robotics','evidence':'Gamma Robotics raised'},
                                    {'name':'Invented Labs','evidence':'not in the source'}])
    researched = []
    monkeypatch.setattr(morning, 'ROOT', project)
    monkeypatch.setattr(morning, 'collect_boards', lambda directory, *a, **k: records)
    monkeypatch.setattr(llm, 'make_provider', lambda cfg: Provider())
    monkeypatch.setattr(exa, 'exa_search', search)
    monkeypatch.setattr(discovery, 'probe_board', lambda name, client: 'https://jobs.ashbyhq.com/gamma' if name == 'Gamma Robotics' else None)
    monkeypatch.setattr(daily_research, 'research_company',
                        lambda row, *a, **k: researched.append(row['Company']) or {'Research Checked At':now_iso()})
    profile = load_profile(Path(__file__).resolve().parents[1]/'profile.yaml.example')
    profile.raw['identity']['experience']['years'] = 5
    out, state = project/'out', project/'state'
    (state/'2026-09-28').mkdir(parents=True)
    summary = morning.daily_work({'morning':{}}, profile, out, state/'2026-09-28', progress=lambda *a:None)
    assert researched == ['beta']
    rows = [r['Company'] for r in __import__('csv').DictReader((out/'Startup Outreach.csv').open(encoding='utf-8-sig'))]
    assert rows == ['beta']
    growth = json.loads((state/'2026-09-28'/'new-companies.json').read_text(encoding='utf-8'))
    assert growth['companies'] == ['Gamma Robotics'] and growth['boards'] == ['https://jobs.ashbyhq.com/gamma']
    assert 'https://jobs.ashbyhq.com/gamma' in json.loads((state/'discovered-boards.json').read_text(encoding='utf-8'))
    assert any(domains == discovery.ATS_DOMAINS for _, domains in searches)
    assert summary['whats_new']['new_jobs'] == 2 and summary['whats_new']['still_open'] == 0
    assert summary['whats_new']['new_companies'] == 2
    assert summary['headline'].startswith('2 new jobs, 2 new companies')
    assert json.loads((state/'company-history.json').read_text(encoding='utf-8'))['beta']['researched']
    # Nothing is written to the real project state by a test run.
    assert (state/'first-seen.json').is_file()


def test_daily_work_finds_verified_cto_email_for_small_startup(tmp_path, monkeypatch):
    import dataclasses
    import json
    from jobagent import llm
    from jobagent.enrich import email_finder, exa
    from jobagent.outreach import daily_research
    from jobagent.runtime import now_iso
    project = tmp_path/'project'
    (project/'resume').mkdir(parents=True)
    (project/'companies.yaml').write_text('ashby: [beta]\n', encoding='utf-8')
    (project/'resume'/'resume_data.py').write_text("NAME='Candidate'\nSUMMARY='QA'\nEXPERIENCE=[{'bullets':['Built Selenium tests.']}]\n", encoding='utf-8')
    job = dataclasses.replace(posting(), company='beta', url='https://jobs.ashbyhq.com/beta/1')
    job.description += ' Qualifications: 5 years of QA experience.'
    record = {'Company':'beta','Vendor':'ashby','Board URL':'https://jobs.ashbyhq.com/beta','Status':'Fetched',
              'Checked At':now_iso(),'Postings':1,'QA titles':1,
              'Source URL':'https://api.ashbyhq.com/posting-api/job-board/beta','jobs':[morning._job_dict(job)]}
    def search(query, *a, **k):
        if k.get('include_domains') == ['linkedin.com'] and 'employees' in query:
            return [{'url':'https://linkedin.com/company/beta','snippet':'# Beta\n- Company Size: 11-50 employees'}]
        if k.get('include_domains') == ['linkedin.com']:
            return [{'url':'https://www.linkedin.com/in/cai','snippet':'# Cai Tech\n\nCo-founder & CTO at Beta'}]
        return []
    finder_calls = []
    def find_emails(people, **k):
        finder_calls.append(people)
        return {('cai', 'tech'): {'email': 'cai@beta.io', 'validationStatus': 'valid', 'overallScore': 97}}
    monkeypatch.setattr(morning, 'ROOT', project)
    monkeypatch.setattr(morning, 'collect_boards', lambda *a, **k: [record])
    monkeypatch.setattr(llm, 'make_provider', lambda cfg: None)
    monkeypatch.setattr(exa, 'exa_search', search)
    monkeypatch.setattr(email_finder, 'find_emails', find_emails)
    monkeypatch.setattr(daily_research, 'research_company', lambda row, *a, **k: {
        'Research Checked At': now_iso(), '_errors': [], '_sources': [{'url': 'https://www.beta.io/about'}]})
    profile = load_profile(Path(__file__).resolve().parents[1]/'profile.yaml.example')
    profile.raw['identity']['experience']['years'] = 5
    out, state = project/'out', project/'state'
    (state/'2026-09-28').mkdir(parents=True)
    paid = {'morning':{}, 'billing':{'allow_paid_services': True}}
    summary = morning.daily_work(paid, profile, out, state/'2026-09-28', progress=lambda *a:None)
    assert finder_calls == [[{'firstName': 'Cai', 'surname': 'Tech', 'domain': 'beta.io'}]]
    rows = list(__import__('csv').DictReader((out/'Startup Outreach.csv').open(encoding='utf-8-sig')))
    assert rows[0]['Public Work Email'] == 'cai@beta.io' and rows[0]['Employee Count'] == '11-50'
    assert rows[0]['Cold Email'].startswith('Hi Cai,')
    assert summary['new_contacts'] == 1 and summary['verified_founder_emails'] == 1
    assert summary['companies_10_to_200_employees'] == 1
    # A second run the same month reuses the lookup instead of paying again.
    (state/'2026-09-29').mkdir()
    morning.daily_work(paid, profile, out, state/'2026-09-29', progress=lambda *a:None)
    assert len(finder_calls) == 1
    # Free-only (the default): no paid lookup, but the CTO is still named.
    (state/'founder-emails.json').unlink()
    (state/'2026-09-30').mkdir()
    morning.daily_work({'morning':{}}, profile, out, state/'2026-09-30', progress=lambda *a:None)
    assert len(finder_calls) == 1
    rows = list(__import__('csv').DictReader((out/'Startup Outreach.csv').open(encoding='utf-8-sig')))
    assert rows[0]['Public Work Email'] == '' and rows[0]['Manager Name'] == 'Cai Tech'


def test_failed_research_is_retried_not_cached(tmp_path):
    from jobagent.outreach.daily_research import research_company
    class Busy:
        def structured(self, *a):
            raise RuntimeError('model busy')
    row = {'Company': 'Example', 'JD Source URL': 'https://jobs.ashbyhq.com/example/1', 'JD Text': 'QA role'}
    data = research_company(row, tmp_path, Busy(), search=lambda *a, **k: [])
    assert data['_errors'] == ['RuntimeError']
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('role,number,phrase',[
    ('Founder and CEO','1','sharing my attached resume'),
    ('Technical Recruiter','3',"I've attached my resume"),
    ('Vice President of Engineering','8','passing my attached resume'),
    ('Chief Technology Officer','4','Is this opening on your team?'),
    ('QA Manager','5','reviewing my attached resume'),
])
def test_user_templates_follow_actual_email_contact(role,number,phrase):
    row={'Company':'Example','Job Title':'QA Engineer','JD Text':'Selenium and API tests'}
    facts={'name':'Candidate','summary':'QA Engineer with 5 years of experience','bullets':['Built Selenium and API tests.'],'resume_attached':True}
    metadata={'Manager Name':'Alex Leader','Manager Role':'CEO','Email Contact Name':'Sam Contact','Email Contact Role':role}
    draft=grounded_draft(row,metadata,facts)
    assert draft['Email Template'].startswith(number+' - ')
    assert draft['Cold Email'].startswith('Hi Sam,') and phrase in draft['Cold Email']
    assert draft['Cold Email'].endswith('Thanks & regards,\nCandidate')
    assert draft['Cold Email Subject'].count('QA Engineer')<=1
    facts['resume_attached']=False
    assert 'attached' not in grounded_draft(row,metadata,facts)['Cold Email']


def test_reviewed_recruiter_survives_old_cache_without_replacing_manager():
    from jobagent.outreach.daily_research import apply_reviewed_contact
    from jobagent.runtime import now_iso
    metadata={'Manager Name':'Alex Leader','Manager Role':'CEO','Email Contact Name':'Old Contact'}
    reviewed={'Public Work Email':'sam@example.com','Email Contact Name':'Sam Recruiter','Email Contact Role':'Technical Recruiter',
              'Email Source':'https://example.com/jobs','Email Evidence':'Public hiring contact','Email Ownership Checked At':now_iso()}
    updated=apply_reviewed_contact(metadata,reviewed)
    assert updated['Manager Name']=='Alex Leader' and updated['Email Contact Name']=='Sam Recruiter'
    assert apply_reviewed_contact(metadata,{**reviewed,'Email Ownership Checked At':'2000-01-01T00:00:00Z'})==metadata
