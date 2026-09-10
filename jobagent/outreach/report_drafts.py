"""Synchronize owned Gmail drafts, preserving edits and never sending."""
import base64
import csv
import hashlib
import json
import re
from email import policy
from email.parser import BytesParser
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit

from jobagent.outreach.gmail import authenticate, get_authenticated_sender, GmailAuthError
from jobagent.runtime import atomic_json, process_lock, now_iso

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / 'gmail_report_drafts.json'


def content_hash(subject, body, to='', attachments=()):
    value = to+'\n'+subject+'\n'+body.strip()
    if attachments:
        value += '\nattachments\n' + json.dumps(sorted(attachments, key=lambda a: (a['filename'], a['sha256'])), sort_keys=True)
    return hashlib.sha256(value.encode()).hexdigest()


def load_resume(path):
    """Read the configured resume once; accept only a bounded PDF or DOCX."""
    path = Path(path)
    mime = {'.pdf':'application/pdf', '.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}.get(path.suffix.lower())
    if not mime or not path.is_file() or not 0 < path.stat().st_size <= 5 * 1024 * 1024:
        raise ValueError('Resume attachment must be an existing PDF or DOCX up to 5 MB')
    data = path.read_bytes()
    if path.suffix.lower() == '.pdf' and not data.startswith(b'%PDF-'):
        raise ValueError('Resume PDF content is invalid')
    if path.suffix.lower() == '.docx':
        import io
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as doc:
                if 'word/document.xml' not in doc.namelist():
                    raise ValueError('Resume DOCX content is invalid')
        except zipfile.BadZipFile as exc:
            raise ValueError('Resume DOCX content is invalid') from exc
    return data, {'filename':path.name, 'mime_type':mime, 'sha256':hashlib.sha256(data).hexdigest()}


def decode_draft(draft):
    raw = base64.urlsafe_b64decode(draft['message']['raw'])
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body_part = message.get_body(preferencelist=('plain',))
    body = body_part.get_content() if body_part else ''
    attachments = [{'filename':part.get_filename() or '', 'mime_type':part.get_content_type(),
                    'sha256':hashlib.sha256(part.get_payload(decode=True) or b'').hexdigest()}
                   for part in message.walk() if not part.is_multipart()
                   and (part.get_filename() or part.get_content_disposition() == 'attachment')]
    return {'key':message.get('X-JobAgent-Draft-Key',''), 'subject':str(message.get('Subject','')),
            'to':str(message.get('To','')), 'body':body.replace('\r\n','\n').strip(), 'attachments':attachments}


def draft_recipient(row):
    """Prefill an exact, sourced public address for review in an unsent draft."""
    address = row.get('Public Work Email', '') or ''
    try:
        source = urlsplit(row.get('Email Source', '') or '')
    except ValueError:
        return ''
    if re.match(r'(?:privacy|security|support|noreply|no-reply|marketing|sales|talent_accommodations)@', address, re.I):
        return ''
    if (re.fullmatch(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', address)
            and source.scheme in ('https', 'http') and source.hostname
            and row.get('Email Evidence')):
        return address
    return ''


def sync_report_drafts(out, expected_sender, *, ledger_path=LEDGER, service=None, deadline=None, recreate_missing=False, resume_path=None):
    if not expected_sender:
        raise ValueError('Expected Gmail account is required')
    resume_data, resume_meta = load_resume(resume_path) if resume_path else (None, None)
    attachments = [resume_meta] if resume_meta else []
    if service is None:
        from googleapiclient.discovery import build
        creds = authenticate(required_scopes=['https://www.googleapis.com/auth/gmail.compose'])
        sender = get_authenticated_sender(creds)
        if sender.casefold() != expected_sender.casefold():
            raise GmailAuthError('wrong_account','Gmail account differs from configured output account')
        service = build('gmail','v1',credentials=creds)
    else:
        sender = service.users().getProfile(userId='me').execute()['emailAddress']
        if sender.casefold() != expected_sender.casefold():
            raise ValueError('Wrong Gmail account')
    out, ledger_path = Path(out), Path(ledger_path)
    rows = list(csv.DictReader((out/'Startup Outreach.csv').open(encoding='utf-8-sig')))
    with process_lock(ledger_path.with_suffix('.lock')):
        ledger = json.loads(ledger_path.read_text(encoding='utf-8')) if ledger_path.exists() else {}
        results = []
        api = service.users().drafts()
        # Recover through saved Gmail IDs and reserved content hashes, without
        # adding custom tracking headers or branding to outgoing messages.
        existing, token = [], None
        while True:
            page = api.list(userId='me',pageToken=token,maxResults=100).execute()
            existing += page.get('drafts',[])
            token = page.get('nextPageToken')
            if not token:
                break
        owned, subjects = {}, set()
        # Gmail's editor can discard custom headers while retaining the draft ID.
        # The saved ID recovers identity; the content hash still protects user edits.
        keys_by_id = {entry['draft_id']: key for key, entry in ledger.items() if entry.get('draft_id')}
        pending_hashes = {entry['pending_content_hash']: key for key, entry in ledger.items()
                          if entry.get('pending_content_hash') and entry.get('state') in ('pending', 'uncertain')}
        for item in existing:
            draft = api.get(userId='me',id=item['id'],format='raw').execute()
            decoded = decode_draft(draft)
            subjects.add(decoded['subject'])
            key = decoded['key'] or keys_by_id.get(draft['id']) or pending_hashes.get(
                content_hash(decoded['subject'], decoded['body'], decoded['to'], decoded['attachments']))
            if key:
                owned[key] = (draft,decoded)
        for row in rows:
            if not row.get('Cold Email') or row.get('Draft Status','').startswith('Withheld'):
                continue
            company = re.sub(r'[^a-z0-9]','',row['Company'].casefold())
            key = hashlib.sha256(company.encode()).hexdigest()[:24]
            subject, body = row['Cold Email Subject'], row['Cold Email']
            if any(c in subject for c in '\r\n'):
                raise ValueError('Invalid draft subject')
            # Draft prefilling does not confirm current ownership or authorize sending.
            to = draft_recipient(row)
            desired_hash = content_hash(subject,body,to,attachments)
            entry = ledger.get(key,{})
            result = {'Company':row['Company'],'Job Link':row['Job Link'],'Status':'',
                      'Gmail Draft URL':'','Recipient':to or 'Public email not available; To left blank',
                      'Email Ownership Status':row.get('Email Ownership Status') or 'unconfirmed',
                      'Email Source':row.get('Email Source',''),
                      'Email Contact':row.get('Email Contact Name') or row.get('Manager Name',''),
                      'Resume Attachment':'',
                      'Recipient Review':'Review public source and current ownership before sending' if to else 'Find a public work email before sending',
                      'Approval':'Pending user approval; unsent'}
            if entry.get('state') == 'sent':
                result.update(Status='Already sent; no duplicate draft created', Recipient=entry.get('sent_to') or to,
                              Approval='Already sent; no new sending action',
                              **{'Sent At':entry.get('sent_at',''), 'Sent Message URL':entry.get('sent_message_url','')})
                results.append(result)
                continue
            if not to:
                result.update(Status='Pending contact research; saved in Excel only', Recipient='')
                results.append(result)
                continue
            if not resume_meta and re.search(r'\b(?:attached\s+(?:my\s+)?resume|resume\s+is\s+attached)\b', body, re.I):
                result['Status'] = 'Withheld: email mentions an attachment but no resume is configured'
                results.append(result)
                continue
            current = owned.get(key)
            actual_attachments = []
            if current:
                draft, decoded = current
                actual_attachments = decoded['attachments']
                actual_hash = content_hash(decoded['subject'],decoded['body'],decoded['to'],actual_attachments)
                if actual_hash == desired_hash:
                    entry.update(state='drafted',draft_id=draft['id'],content_hash=actual_hash)
                    result['Status'] = 'Existing draft reused'
                elif entry.get('content_hash') != actual_hash:
                    result['Status'] = 'User edits or uncertain ownership preserved; review existing draft'
                    result['Recipient'] = decoded['to'] or 'Existing draft To is blank'
                    result['Recipient Review'] = 'Existing recipient preserved; review before sending'
                else:
                    result['Status'] = 'Update needed'
            elif entry and not recreate_missing:
                result['Status'] = 'Previously reserved draft absent; not recreated (may have been sent/deleted)'
            elif subject in subjects:
                result['Status'] = 'Similar existing draft preserved; no duplicate created'
            else:
                result['Status'] = 'Create needed'
            if result['Status'] in ('Create needed','Update needed'):
                from datetime import datetime, timezone
                if deadline and datetime.now(timezone.utc) >= deadline:
                    raise TimeoutError('Morning draft window ended')
                message = EmailMessage()
                message['From'], message['Subject'] = sender, subject
                if to:
                    message['To'] = to
                message.set_content(body)
                if resume_meta:
                    maintype, subtype = resume_meta['mime_type'].split('/', 1)
                    message.add_attachment(resume_data, maintype=maintype, subtype=subtype, filename=resume_meta['filename'])
                payload = {'message':{'raw':base64.urlsafe_b64encode(message.as_bytes()).decode()}}
                updating = result['Status'] == 'Update needed'
                if entry.get('draft_id') and not updating:
                    entry.setdefault('replaced_draft_ids', []).append(entry['draft_id'])
                    entry['recreation_reason'] = 'User explicitly requested rebuilding deleted drafts'
                entry.update(state='pending',company=row['Company'],reserved_at=now_iso(),pending_content_hash=desired_hash)
                ledger[key] = entry
                atomic_json(ledger_path,ledger)
                try:
                    saved = api.update(userId='me',id=current[0]['id'],body=payload).execute() if updating else api.create(userId='me',body=payload).execute()
                    decoded = decode_draft(api.get(userId='me',id=saved['id'],format='raw').execute())
                    if content_hash(decoded['subject'],decoded['body'],decoded['to'],decoded['attachments']) != desired_hash:
                        raise ValueError('Draft read-back differs')
                    actual_attachments = decoded['attachments']
                    entry.update(state='drafted',draft_id=saved['id'],content_hash=desired_hash,verified_at=now_iso())
                    result['Status'] = 'Updated and read back' if updating else 'Created and read back'
                except Exception as exc:
                    entry.update(state='uncertain',error=type(exc).__name__)
                    result['Status'] = 'Uncertain draft result; no automatic duplicate retry'
            if current or entry.get('draft_id'):
                result['Gmail Draft URL'] = 'https://mail.google.com/mail/u/0/#drafts'
            result['Resume Attachment'] = '; '.join(a['filename'] for a in actual_attachments)
            ledger[key] = entry
            atomic_json(ledger_path,ledger)
            results.append(result)
        atomic_json(out/'Gmail Draft Status.json',{'checked_at':now_iso(),'sender':sender,'outreach_sent':0,'drafts':results})
        return results
