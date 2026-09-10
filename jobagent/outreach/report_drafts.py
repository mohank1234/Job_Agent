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


def content_hash(subject, body, to=''):
    return hashlib.sha256((to+'\n'+subject+'\n'+body.strip()).encode()).hexdigest()


def decode_draft(draft):
    raw = base64.urlsafe_b64decode(draft['message']['raw'])
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body = message.get_body(preferencelist=('plain',)).get_content() if message.is_multipart() else message.get_content()
    return {'key':message.get('X-JobAgent-Draft-Key',''), 'subject':str(message.get('Subject','')),
            'to':str(message.get('To','')), 'body':body.replace('\r\n','\n').strip()}


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


def sync_report_drafts(out, expected_sender, *, ledger_path=LEDGER, service=None, deadline=None, recreate_missing=False):
    if not expected_sender:
        raise ValueError('Expected Gmail account is required')
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
                content_hash(decoded['subject'], decoded['body'], decoded['to']))
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
            desired_hash = content_hash(subject,body,to)
            entry = ledger.get(key,{})
            result = {'Company':row['Company'],'Job Link':row['Job Link'],'Status':'',
                      'Gmail Draft URL':'','Recipient':to or 'Public email not available; To left blank',
                      'Email Ownership Status':row.get('Email Ownership Status') or 'unconfirmed',
                      'Email Source':row.get('Email Source',''),
                      'Email Contact':row.get('Email Contact Name') or row.get('Manager Name',''),
                      'Recipient Review':'Review public source and current ownership before sending' if to else 'Find a public work email before sending',
                      'Approval':'Pending user approval; unsent'}
            if not to:
                result.update(Status='Pending contact research; saved in Excel only', Recipient='')
                results.append(result)
                continue
            current = owned.get(key)
            if current:
                draft, decoded = current
                actual_hash = content_hash(decoded['subject'],decoded['body'],decoded['to'])
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
                    if content_hash(decoded['subject'],decoded['body'],decoded['to']) != desired_hash:
                        raise ValueError('Draft read-back differs')
                    entry.update(state='drafted',draft_id=saved['id'],content_hash=desired_hash,verified_at=now_iso())
                    result['Status'] = 'Updated and read back' if updating else 'Created and read back'
                except Exception as exc:
                    entry.update(state='uncertain',error=type(exc).__name__)
                    result['Status'] = 'Uncertain draft result; no automatic duplicate retry'
            if current or entry.get('draft_id'):
                result['Gmail Draft URL'] = 'https://mail.google.com/mail/u/0/#drafts'
            ledger[key] = entry
            atomic_json(ledger_path,ledger)
            results.append(result)
        atomic_json(out/'Gmail Draft Status.json',{'checked_at':now_iso(),'sender':sender,'outreach_sent':0,'drafts':results})
        return results
