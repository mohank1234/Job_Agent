import base64
import csv
import pytest
from email.message import EmailMessage
from types import SimpleNamespace

from jobagent.morning import experience_focus
from jobagent.outreach.verification import canonical, employer_posting, board_ref
from jobagent.report_quality import deduplicate_opportunities, review_frameworks
from jobagent.startup_output import unique_notes
from jobagent.outreach.report_drafts import sync_report_drafts, draft_recipient, decode_draft
from tests.test_regressions import posting


def test_external_greenhouse_ids_and_eu():
    job = posting()
    job.url = 'https://example.com/careers?gh_jid=123'
    job.raw = {'id':123,'absolute_url':job.url}
    fixed = employer_posting(job,'greenhouse','example')
    assert fixed.url == 'https://job-boards.greenhouse.io/example/jobs/123'
    assert fixed.raw['employer_url'] == job.url
    assert canonical(job.url) != canonical('https://example.com/careers?gh_jid=456')
    assert board_ref('https://job-boards.eu.greenhouse.io/example/jobs/123') == ('greenhouse','example')


def test_experience_wording_and_duplicate_notes():
    assert experience_focus('Qualifications: 6+ years of Automated Testing frameworks experience. Proficiency in Selenium and Java.')[0]=='4-6 year minimum'
    assert experience_focus('What makes a great candidate: 5+ years in quality engineering, test automation, or software engineering.')[0]=='4-6 year minimum'
    assert unique_notes('QA role; gap; QA role','gap; more')=='QA role; gap; more'


def test_corrected_boards_and_phonepe_public_status(monkeypatch):
    from jobagent import morning
    directory=morning.board_directory({'greenhouse':['phonepe','ocrolus']})
    assert set(directory)=={('phonepe','phonepe'),('greenhouse','ocrolusinc')}
    feed={'results':[{'status':status,'applyUrl':'https://careers.example/job','title':'QA Engineer','location':'India'}
                     for status in ['PUBLIC','INTERNAL','PRIVATE','NOT_PUBLISHED']]}
    monkeypatch.setattr(morning,'get_public',lambda *a:SimpleNamespace(json=lambda:feed))
    jobs,url=morning.phonepe_board(None)
    assert len(jobs)==1
    assert jobs[0].description==''  # metadata does not pretend to be a full JD
    assert jobs[0].raw['status']=='PUBLIC'


def test_identical_opportunities_keep_locations_and_distinct_jobs():
    row={'Company':'Example','Job Title':'SDET','JD Text':'Same full description','Job Link':'a','Location':'Pune','Fit Status':'in_scope'}
    unique,dup=deduplicate_opportunities([row,{**row,'Job Link':'b','Location':'Bengaluru'},
                                        {**row,'Job Link':'c','JD Text':'Different responsibilities'}])
    assert len(unique)==2 and len(dup)==1
    assert unique[0]['Location Variants']=='Pune | Bengaluru'
    assert unique[0]['Duplicate Listing URLs']=='a | b'


def test_listing_playwright_does_not_prove_typescript_depth():
    row={'JD Text':'Requirements: strong TypeScript and Playwright framework ownership.','Fit Status':'in_scope','Fit Notes':'QA'}
    review_frameworks(row,'Playwright, JavaScript (basics), Selenium')
    assert row['Fit Status']=='needs_review'
    assert 'not established in resume' in row['Framework Review']
    assert 'production suite' in row['Framework Review']


class FakeGmail:
    def __init__(self):
        self.data={}
        self.created=0
        self.updated=0
    def users(self):return self
    def drafts(self):return self
    def response(self,value):return SimpleNamespace(execute=lambda:value)
    def getProfile(self,**kwargs):return self.response({'emailAddress':'candidate@example.com'})
    def list(self,**kwargs):return self.response({'drafts':[{'id':k} for k in self.data]})
    def get(self,id,**kwargs):return self.response(self.data[id])
    def create(self,body,**kwargs):
        self.created+=1
        key=str(self.created)
        self.data[key]={'id':key,'message':{**body['message'],'id':'message-'+key}}
        return self.response(self.data[key])
    def update(self,id,body,**kwargs):
        self.updated+=1
        self.data[id]['message'].update(body['message'])
        return self.response(self.data[id])


def test_gmail_drafts_reuse_and_preserve_edits(tmp_path):
    row={'Company':'Example','Job Link':'https://jobs.example/1','Cold Email Subject':'SDET at Example',
         'Cold Email':'Hi Alex,\n\nI built API tests. Could we talk?','Public Work Email':'historical@example.com',
         'Email Source':'https://example.com/team','Email Evidence':'Public hiring contact','Draft Status':'Draft only'}
    with (tmp_path/'Startup Outreach.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
    service=FakeGmail()
    kwargs={'ledger_path':tmp_path/'ledger.json','service':service}
    first=sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)
    assert first[0]['Status']=='Created and read back'
    from jobagent.outreach.report_drafts import decode_draft
    assert decode_draft(service.data['1'])['to']=='historical@example.com'
    assert sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]['Status']=='Existing draft reused'
    assert service.created==1
    decoded=decode_draft(service.data['1'])
    message=EmailMessage();message['Subject']=decoded['subject'];message['X-JobAgent-Draft-Key']=decoded['key']
    message.set_content('My personal edits')
    service.data['1']['message']['raw']=base64.urlsafe_b64encode(message.as_bytes()).decode()
    assert 'preserved' in sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]['Status']
    assert service.updated==0
    service.data.clear()
    assert 'not recreated' in sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]['Status']
    assert service.created==1


def test_sourced_recipient_and_signature_update_existing_draft(tmp_path):
    row={'Company':'Example','Job Link':'https://jobs.example/1','Cold Email Subject':'SDET at Example',
         'Cold Email':'Hi Alex,\n\nI built API tests.\n\nThanks,\nCandidate', 'Public Work Email':'alex@example.com',
         'Email Source':'https://example.com/team','Email Evidence':'Public historical hiring contact',
         'Email Ownership Status':'unconfirmed','Draft Status':'Draft only'}
    def save():
        with (tmp_path/'Startup Outreach.csv').open('w',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
    service=FakeGmail()
    kwargs={'ledger_path':tmp_path/'ledger.json','service':service}
    save()
    sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)
    # Opening/saving in Gmail can strip our custom header without changing content.
    decoded=decode_draft(service.data['1'])
    message=EmailMessage();message['Subject']=decoded['subject'];message['To']=decoded['to'];message.set_content(decoded['body'])
    service.data['1']['message']['raw']=base64.urlsafe_b64encode(message.as_bytes()).decode()
    row['Public Work Email']='alex@example.com'
    row['Cold Email']+='\n5550101234'
    save()
    result=sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]
    assert result['Status']=='Updated and read back'
    assert result['Email Ownership Status']=='unconfirmed'
    assert decode_draft(service.data['1'])['to']=='alex@example.com'
    assert decode_draft(service.data['1'])['body'].endswith('Candidate\n5550101234')
    assert sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]['Status']=='Existing draft reused'
    assert service.created==1 and service.updated==1
    service.data.clear()
    assert 'not recreated' in sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]['Status']
    assert sync_report_drafts(tmp_path,'candidate@example.com',recreate_missing=True,**kwargs)[0]['Status']=='Created and read back'
    assert service.created==2


def test_no_gmail_draft_or_reservation_without_sourced_recipient(tmp_path):
    row={'Company':'Example','Job Link':'https://jobs.example/1','Cold Email Subject':'QA opportunity',
         'Cold Email':'Hi Alex','Public Work Email':'guessed@example.com'}
    with (tmp_path/'Startup Outreach.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
    service=FakeGmail()
    ledger=tmp_path/'ledger.json'
    result=sync_report_drafts(tmp_path,'candidate@example.com',ledger_path=ledger,service=service)
    assert result[0]['Status']=='Pending contact research; saved in Excel only'
    assert service.created==0 and service.updated==0
    assert not ledger.exists()


def test_headerless_draft_recovers_after_lost_create_response(tmp_path):
    row={'Company':'Example','Job Link':'https://jobs.example/1','Cold Email Subject':'QA opportunity',
         'Cold Email':'Hi Alex,\n\nCould we connect?','Public Work Email':'alex@example.com',
         'Email Source':'https://example.com/team','Email Evidence':'Public hiring contact'}
    with (tmp_path/'Startup Outreach.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
    service=FakeGmail();normal_create=service.create
    def lost_response(**kwargs):
        def execute():
            normal_create(**kwargs).execute()
            raise TimeoutError('Saved remotely; response lost')
        return SimpleNamespace(execute=execute)
    service.create=lost_response
    resume=tmp_path/'Resume.pdf';resume.write_bytes(b'%PDF-1.4\nresume fixture\n%%EOF')
    kwargs={'ledger_path':tmp_path/'ledger.json','service':service,'resume_path':resume}
    assert 'Uncertain' in sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]['Status']
    assert decode_draft(service.data['1'])['key']==''
    assert sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]['Status']=='Existing draft reused'
    assert service.created==1


def test_resume_readback_updates_and_preserves_user_attachments(tmp_path):
    import hashlib
    row={'Company':'Example','Job Link':'https://jobs.example/1','Cold Email Subject':'QA opening',
         'Cold Email':"Hi Alex,\nI've attached my resume.",'Public Work Email':'alex@example.com',
         'Email Source':'https://example.com/team','Email Evidence':'Public hiring contact'}
    with (tmp_path/'Startup Outreach.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
    service=FakeGmail();kwargs={'ledger_path':tmp_path/'ledger.json','service':service}
    assert 'Withheld' in sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]['Status']
    assert service.created==0
    resume=tmp_path/'Resume.pdf';resume.write_bytes(b'%PDF-1.4\nresume one\n%%EOF')
    kwargs['resume_path']=resume
    result=sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]
    assert result['Status']=='Created and read back' and result['Resume Attachment']=='Resume.pdf'
    decoded=decode_draft(service.data['1'])
    assert decoded['attachments']==[{'filename':'Resume.pdf','mime_type':'application/pdf','sha256':hashlib.sha256(resume.read_bytes()).hexdigest()}]
    assert sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]['Status']=='Existing draft reused'
    resume.write_bytes(b'%PDF-1.4\nresume two\n%%EOF')
    assert sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]['Status']=='Updated and read back'
    decoded=decode_draft(service.data['1'])
    message=EmailMessage();message['To']=decoded['to'];message['Subject']=decoded['subject'];message.set_content(decoded['body'])
    message.add_attachment(b'Personal notes',maintype='text',subtype='plain',filename='My-notes.txt')
    service.data['1']['message']['raw']=base64.urlsafe_b64encode(message.as_bytes()).decode()
    result=sync_report_drafts(tmp_path,'candidate@example.com',**kwargs)[0]
    assert 'preserved' in result['Status'] and result['Resume Attachment']=='My-notes.txt'
    assert service.created==1 and service.updated==1


def test_confirmed_sent_contact_never_recreated(tmp_path):
    import hashlib,json
    row={'Company':'Example','Job Link':'https://jobs.example/1','Cold Email Subject':'QA opening',
         'Cold Email':"Hi Alex, I've attached my resume.",'Public Work Email':'alex@example.com',
         'Email Source':'https://example.com/team','Email Evidence':'Public hiring contact'}
    with (tmp_path/'Startup Outreach.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
    key=hashlib.sha256(b'example').hexdigest()[:24]
    ledger=tmp_path/'ledger.json';ledger.write_text(json.dumps({key:{'state':'sent','sent_to':'alex@example.com','sent_at':'2026-09-10T02:24:51Z'}}))
    service=FakeGmail()
    result=sync_report_drafts(tmp_path,'candidate@example.com',ledger_path=ledger,service=service,recreate_missing=True)[0]
    assert result['Status']=='Already sent; no duplicate draft created'
    assert result['Approval']=='Already sent; no new sending action'
    assert service.created==0 and service.updated==0


@pytest.mark.parametrize('changes',[
    {'Public Work Email':'alex@example.com\nBcc: other@example.com'},
    {'Public Work Email':'Alex <alex@example.com>'},
    {'Public Work Email':'talent_accommodations@example.com'},
    {'Email Source':''}, {'Email Source':'file:///private/contact'}, {'Email Source':'https://['}, {'Email Evidence':''},
])
def test_recipient_requires_exact_address_and_public_evidence(changes):
    row={'Public Work Email':'alex@example.com','Email Source':'https://example.com/team','Email Evidence':'Public record'}
    assert draft_recipient({**row,**changes})==''
