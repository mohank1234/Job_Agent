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


def content_hash(subject, body, to='', attachments=(), html=False):
    value = to+'\n'+subject+'\n'+body.strip()
    # A formatted (HTML) draft differs from a plain-text-only one, so drafts
    # made before HTML bodies are updated once. Plain text adds no marker, so
    # hashes stored before this change stay valid.
    if html:
        value += '\nformat:html'
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
    html = any(part.get_content_type() == 'text/html' for part in message.walk() if not part.is_multipart())
    return {'key':message.get('X-JobAgent-Draft-Key',''), 'subject':str(message.get('Subject','')),
            'to':str(message.get('To','')), 'body':body.replace('\r\n','\n').strip(), 'attachments':attachments,
            'html':html}


def html_body(body):
    """The email as Gmail's own editor stores it: each paragraph a block and
    line breaks kept (sign-off), so it flows to any window width instead of
    being hard-wrapped the way plain text is."""
    import html as html_lib
    paragraphs = [html_lib.escape(p.strip()).replace('\n', '<br>') for p in body.strip().split('\n\n') if p.strip()]
    return '<div dir="ltr">' + '<div><br></div>'.join(f'<div>{p}</div>' for p in paragraphs) + '</div>'


def draft_recipient(row):
    """Prefill an exact, sourced public address for review in an unsent draft."""
    from jobagent.startup_output import opening_flag, outreach_domain
    if opening_flag(row) is not None:
        from jobagent.outreach.founders import verified_leadership_contact
        from jobagent.outreach.service import recent
        if (not verified_leadership_contact(row, outreach_domain(row))
                or not recent(row.get('Email Ownership Checked At'), max_age_days=30)):
            return ''
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


def extract_signature(body):
    """Best-effort sign-off block: the text after the last blank line."""
    parts = body.strip().split('\n\n')
    return parts[-1] if len(parts) > 1 else ''


# Only these draft outcomes reflect content this app generated and verified
# unchanged; anything else (preserved user edits, uncertain results) is left
# for manual review even when auto-send is enabled.
SEND_ELIGIBLE_STATUSES = {'Created and read back', 'Updated and read back', 'Existing draft reused'}


def matching_ledger_key(row, ledger):
    """Preserve company-level suppression, also across renamed firms/inboxes."""
    from jobagent.startup_output import outreach_domain
    company = re.sub(r'[^a-z0-9]', '', row.get('Company', '').casefold())
    legacy = hashlib.sha256(company.encode()).hexdigest()[:24]
    domain = outreach_domain(row)
    email = (row.get('Public Work Email') or '').strip().casefold()
    for key, entry in ledger.items():
        prior_company = re.sub(r'[^a-z0-9]', '', str(entry.get('company', '')).casefold())
        prior_email = (entry.get('sent_to') or entry.get('email') or '').strip().casefold()
        prior_domain = str(entry.get('domain') or '').casefold().removeprefix('www.')
        if (key == legacy or company and company == prior_company
                or email and email == prior_email or domain and domain == prior_domain):
            return key
    return None


def _followup_status(entry):
    if entry.get('followup_sent_at'):
        return 'Sent ' + entry['followup_sent_at']
    if entry.get('followup_skipped_reason'):
        return 'Skipped: ' + entry['followup_skipped_reason']
    return 'Not scheduled'


def sync_report_drafts(out, expected_sender, *, ledger_path=LEDGER, service=None, deadline=None, recreate_missing=False, resume_path=None, auto_send=False):
    if not expected_sender:
        raise ValueError('Expected Gmail account is required')
    resume_data, resume_meta = load_resume(resume_path) if resume_path else (None, None)
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
    from jobagent.startup_output import duplicate_key, opening_flag, outreach_domain, tracker_row, write_outreach
    with process_lock(ledger_path.with_suffix('.lock')):
        ledger = json.loads(ledger_path.read_text(encoding='utf-8')) if ledger_path.exists() else {}
        historical_path = ledger_path.parent / 'outreach_draft_ledger.json'
        historical = json.loads(historical_path.read_text(encoding='utf-8')) if historical_path.exists() else {}
        if any(not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()) for data in (ledger, historical)):
            raise ValueError('Invalid outreach ledger; refusing to risk duplicate drafts')
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
        owned, subjects, recipients = {}, set(), set()
        # Gmail's editor can discard custom headers while retaining the draft ID.
        # The saved ID recovers identity; the content hash still protects user edits.
        keys_by_id = {entry['draft_id']: key for key, entry in ledger.items() if entry.get('draft_id')}
        pending_hashes = {entry['pending_content_hash']: key for key, entry in ledger.items()
                          if entry.get('pending_content_hash') and entry.get('state') in ('pending', 'uncertain')}
        for item in existing:
            draft = api.get(userId='me',id=item['id'],format='raw').execute()
            decoded = decode_draft(draft)
            subjects.add(decoded['subject'])
            if decoded['to']:
                from email.utils import getaddresses
                recipients.update(address.casefold() for _, address in getaddresses([decoded['to']]))
            key = decoded['key'] or keys_by_id.get(draft['id']) or pending_hashes.get(
                content_hash(decoded['subject'], decoded['body'], decoded['to'], decoded['attachments'], decoded['html']))
            if key:
                owned[key] = (draft,decoded)
        processed = set()
        for row in rows:
            if not row.get('Cold Email') or row.get('Draft Status','').startswith('Withheld'):
                continue
            company = re.sub(r'[^a-z0-9]','',row['Company'].casefold())
            key = matching_ledger_key(row, ledger) or hashlib.sha256(company.encode()).hexdigest()[:24]
            subject, body = row['Cold Email Subject'], row['Cold Email']
            if any(c in subject for c in '\r\n'):
                raise ValueError('Invalid draft subject')
            # Draft prefilling does not confirm current ownership or authorize sending.
            to = draft_recipient(row)
            # The resume tailored to this job description when one was built,
            # otherwise the configured resume.
            row_data, row_meta = resume_data, resume_meta
            if row.get('Resume File'):
                tailored_path = (out / row['Resume File']).resolve()
                if not tailored_path.is_relative_to(out.resolve()):
                    raise ValueError('Tailored resume must stay inside the report directory')
                if not tailored_path.is_file():
                    row_data, row_meta = None, None
                else:
                    row_data, row_meta = load_resume(tailored_path)
            desired_hash = content_hash(subject,body,to,[row_meta] if row_meta else [],html=True)
            entry = ledger.get(key,{})
            result = {'Company':row['Company'],'Job Link':row.get('Job Link',''),'Status':'',
                      'Gmail Draft URL':'','Recipient':to or 'Public email not available; To left blank',
                      'Email Ownership Status':row.get('Email Ownership Status') or 'unconfirmed',
                      'Email Source':row.get('Email Source',''),
                      'Email Contact':row.get('Email Contact Name') or row.get('Manager Name',''),
                      'Resume Attachment':'', 'Duplicate Key':duplicate_key(row),
                      'Date Created':entry.get('reserved_at') or row.get('Date Created') or now_iso(),
                      'Sent At':entry.get('sent_at',''), 'Follow-up Status':_followup_status(entry),
                      'Recipient Review':'Review public source and current ownership before sending' if to else 'Find a public work email before sending',
                      'Approval':'Pending user approval; unsent'}
            if entry.get('state') == 'sent':
                result.update(Status='Already sent; no duplicate draft created', Recipient=entry.get('sent_to') or to,
                              Approval='Already sent; no new sending action',
                              **{'Sent At':entry.get('sent_at',''), 'Sent Message URL':entry.get('sent_message_url','')})
                results.append(result)
                continue
            if (key in processed or entry.get('state') in ('sending', 'send_uncertain')
                    or entry.get('job_url') and entry['job_url'] != row.get('Job Link')):
                result['Status'] = 'Existing company/contact outreach preserved; no duplicate created'
                results.append(result)
                continue
            if matching_ledger_key(row, historical) is not None:
                result['Status'] = 'Historical outreach already reserved or drafted; no duplicate created'
                results.append(result)
                continue
            # Only outreach with a sourced address becomes a Gmail draft; the
            # rest stays in the Excel report (user choice 2026-09-28: no
            # blank-To drafts).
            if not to:
                result.update(Status='Pending contact research; saved in Excel only', Recipient='')
                results.append(result)
                continue
            processed.add(key)
            if row.get('Resume File') and row_meta is None:
                result['Status'] = 'Withheld: selected tailored resume is missing'
                results.append(result)
                continue
            if not row_meta and re.search(r'\b(?:attached\s+(?:my\s+)?resume|resume\s+is\s+attached)\b', body, re.I):
                result['Status'] = 'Withheld: email mentions an attachment but no resume is configured'
                results.append(result)
                continue
            current = owned.get(key)
            actual_attachments = []
            if current:
                draft, decoded = current
                actual_attachments = decoded['attachments']
                actual_hash = content_hash(decoded['subject'],decoded['body'],decoded['to'],actual_attachments,decoded['html'])
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
            elif subject in subjects or to.casefold() in recipients:
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
                message.add_alternative(html_body(body), subtype='html')
                if row_meta:
                    maintype, subtype = row_meta['mime_type'].split('/', 1)
                    message.add_attachment(row_data, maintype=maintype, subtype=subtype, filename=row_meta['filename'])
                payload = {'message':{'raw':base64.urlsafe_b64encode(message.as_bytes()).decode()}}
                updating = result['Status'] == 'Update needed'
                if entry.get('draft_id') and not updating:
                    entry.setdefault('replaced_draft_ids', []).append(entry['draft_id'])
                    entry['recreation_reason'] = 'User explicitly requested rebuilding deleted drafts'
                entry.update(state='pending', company=row['Company'], email=to, domain=outreach_domain(row),
                             reserved_at=entry.get('reserved_at') or now_iso(), pending_content_hash=desired_hash,
                             job_url=row.get('Job Link',''), role=row.get('Job Title',''),
                             has_relevant_opening=opening_flag(row), duplicate_key=duplicate_key(row),
                             contact_name=row.get('Email Contact Name') or row.get('Manager Name',''),
                             contact_title=row.get('Email Contact Role') or row.get('Manager Role',''),
                             email_source=row.get('Email Source',''), email_provider=row.get('Email Provider',''),
                             verification_status=row.get('Email Verification Status') or row.get('Email Ownership Status',''),
                             startup_source=row.get('Startup Source',''), team_size=row.get('Team Size') or row.get('Employee Count',''),
                             region=row.get('Region') or row.get('Location',''), resume_filename=row_meta['filename'] if row_meta else '',
                             subject=subject)
                ledger[key] = entry
                atomic_json(ledger_path,ledger)
                try:
                    saved = api.update(userId='me',id=current[0]['id'],body=payload).execute() if updating else api.create(userId='me',body=payload).execute()
                    decoded = decode_draft(api.get(userId='me',id=saved['id'],format='raw').execute())
                    if content_hash(decoded['subject'],decoded['body'],decoded['to'],decoded['attachments'],decoded['html']) != desired_hash:
                        raise ValueError('Draft read-back differs')
                    actual_attachments = decoded['attachments']
                    owned[key] = (saved, decoded)
                    subjects.add(subject)
                    recipients.add(to.casefold())
                    entry.update(state='drafted',draft_id=saved['id'],content_hash=desired_hash,verified_at=now_iso())
                    result['Status'] = 'Updated and read back' if updating else 'Created and read back'
                except Exception as exc:
                    entry.update(state='uncertain',error=type(exc).__name__)
                    result['Status'] = 'Uncertain draft result; no automatic duplicate retry'
            if (auto_send and result['Status'] in SEND_ELIGIBLE_STATUSES
                    and entry.get('draft_id') and entry.get('state') == 'drafted' and to):
                try:
                    entry.update(state='sending', send_attempted_at=now_iso())
                    atomic_json(ledger_path, ledger)
                    sent = api.send(userId='me', body={'id': entry['draft_id']}).execute()
                    headers = service.users().messages().get(
                        userId='me', id=sent['id'], format='metadata', metadataHeaders=['Message-ID']
                    ).execute().get('payload', {}).get('headers', [])
                    rfc_message_id = next((h['value'] for h in headers if h['name'] == 'Message-ID'), '')
                    entry.update(state='sent', sent_at=now_iso(), sent_to=to, sent_message_id=sent['id'],
                                 sent_message_url=f"https://mail.google.com/mail/#all/{sent['id']}",
                                 thread_id=sent.get('threadId', ''), rfc_message_id=rfc_message_id,
                                 subject=subject, role=row.get('Job Title', ''), signature=extract_signature(body))
                    result.update(Status='Sent automatically (auto-send enabled)',
                                  Approval='Sent automatically; no manual review step')
                except Exception as exc:
                    entry.update(state='send_uncertain', error=type(exc).__name__)
                    result['Status'] = 'Auto-send outcome uncertain; manual review required before retry'
            if entry.get('state') == 'sent':
                result['Gmail Draft URL'] = entry.get('sent_message_url', '')
            elif current or entry.get('draft_id'):
                result['Gmail Draft URL'] = 'https://mail.google.com/mail/u/0/#drafts'
            result['Resume Attachment'] = '; '.join(a['filename'] for a in actual_attachments)
            result['Sent At'] = entry.get('sent_at','')
            ledger[key] = entry
            atomic_json(ledger_path,ledger)
            results.append(result)
        outreach_sent = sum(1 for r in results if r['Status'] == 'Sent automatically (auto-send enabled)')
        atomic_json(out/'Gmail Draft Status.json',{'checked_at':now_iso(),'sender':sender,'outreach_sent':outreach_sent,'drafts':results})
        states = {r.get('Job Link'):r for r in results}
        write_outreach(out, [tracker_row(row, states.get(row.get('Job Link'))) for row in rows])
        return results
