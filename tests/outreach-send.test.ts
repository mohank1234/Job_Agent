import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium, type Browser, type Page } from 'playwright';
import { processOutreachContact, connectTimings } from '../src/linkedin-connect.js';
import { sendPreparedOutreach } from '../src/outreach-send.js';
import { openDatabase } from '../src/database.js';

let browser: Browser;
let root: string;
before(async () => { connectTimings.inviteDialogMs = 400; root = await mkdtemp(join(tmpdir(), 'job-link-outreach-send-')); browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); await rm(root, { recursive: true, force: true }); });

const profile = (opts: { heading?: string; actions?: string; extra?: string } = {}): string => `<!doctype html><html><head><title>LinkedIn</title></head><body>
<h1>${opts.heading ?? 'Asha Kapoor'}</h1>
${opts.extra ?? ''}
${opts.actions ?? '<button type="button" id="connect-btn">Connect</button>'}
<div role="dialog" id="connect-dialog" style="display:none">
  <h2>Add a note to your invitation?</h2>
  <button type="button" id="add-note-btn">Add a note</button>
  <button type="button" id="send-without-btn">Send without a note</button>
  <div id="note-area" style="display:none">
    <textarea aria-label="Add a note" id="note-textarea"></textarea>
    <button type="button" id="send-invite-btn">Send invitation</button>
  </div>
</div>
<script>
document.getElementById('connect-btn')?.addEventListener('click', () => { document.getElementById('connect-dialog').style.display='block'; });
document.getElementById('add-note-btn')?.addEventListener('click', () => { document.getElementById('note-area').style.display='block'; document.getElementById('add-note-btn').style.display='none'; document.getElementById('send-without-btn').style.display='none'; });
document.getElementById('send-invite-btn')?.addEventListener('click', () => { document.body.setAttribute('data-sent','true'); });
document.getElementById('more-btn')?.addEventListener('click', () => { const m=document.getElementById('more-menu'); m.style.display = m.style.display==='block' ? 'none' : 'block'; });
document.getElementById('connect-menuitem')?.addEventListener('click', () => { document.getElementById('more-menu').style.display='none'; document.getElementById('connect-dialog').style.display='block'; });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { const m=document.getElementById('more-menu'); if (m) m.style.display='none'; } });
</script>
</body></html>`;

async function open(html: string): Promise<Page> {
  const page = await browser.newPage();
  await page.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: html }));
  await page.goto('https://www.linkedin.com/in/asha-kapoor');
  return page;
}

test('note is filled and left for a human to send; the real Send button is never clicked', async () => {
  const page = await open(profile());
  const result = await processOutreachContact(page, root, 'Asha Kapoor', page.url(), "Hi Asha, I'd appreciate connecting.", 'test-happy');
  assert.equal(result.status, 'READY_TO_SEND', result.reason);
  assert.equal(await page.locator('#note-textarea').inputValue(), "Hi Asha, I'd appreciate connecting.");
  assert.equal(await page.evaluate(() => document.body.getAttribute('data-sent')), null);
  await page.close();
});

test('an already-connected profile is recognized without opening the invitation dialog', async () => {
  const page = await open(profile({ actions: '<button type="button" id="message-btn">Message</button>' }));
  const result = await processOutreachContact(page, root, 'Asha Kapoor', page.url(), 'Hi Asha', 'test-connected');
  assert.equal(result.status, 'ALREADY_CONNECTED');
  assert.equal(await page.locator('#connect-dialog').evaluate((el) => getComputedStyle(el).display), 'none');
  await page.close();
});

test('a pending invitation is recognized and not resent', async () => {
  const page = await open(profile({ actions: '<button type="button" id="pending-btn">Pending</button>' }));
  const result = await processOutreachContact(page, root, 'Asha Kapoor', page.url(), 'Hi Asha', 'test-pending');
  assert.equal(result.status, 'PENDING');
  await page.close();
});

test('Connect hidden behind a More menu is still found and used', async () => {
  const actions = '<button type="button" id="message-btn-hidden" style="display:none">Message</button><button type="button" id="more-btn">More actions</button><div role="menu" id="more-menu" style="display:none"><div role="menuitem" id="connect-menuitem" tabindex="0">Connect</div></div>';
  const page = await open(profile({ actions }));
  const result = await processOutreachContact(page, root, 'Asha Kapoor', page.url(), 'Hi Asha, via the overflow menu.', 'test-more-menu');
  assert.equal(result.status, 'READY_TO_SEND', result.reason);
  assert.equal(await page.locator('#note-textarea').inputValue(), 'Hi Asha, via the overflow menu.');
  await page.close();
});

test('a profile name mismatch is never connected to', async () => {
  const page = await open(profile({ heading: 'Someone Else Entirely' }));
  const result = await processOutreachContact(page, root, 'Asha Kapoor', page.url(), 'Hi Asha', 'test-mismatch');
  assert.equal(result.status, 'NEEDS_USER_INPUT');
  assert.match(result.reason!, /does not match/);
  assert.equal(await page.locator('#connect-dialog').evaluate((el) => getComputedStyle(el).display), 'none');
  await page.close();
});

test('a login wall stops the flow before any click is attempted', async () => {
  const page = await open(profile({ extra: '<label>Password<input type="password"></label>' }));
  const result = await processOutreachContact(page, root, 'Asha Kapoor', page.url(), 'Hi Asha', 'test-login');
  assert.equal(result.status, 'LOGIN_REQUIRED');
  assert.equal(await page.locator('#connect-dialog').evaluate((el) => getComputedStyle(el).display), 'none');
  await page.close();
});

test('a security challenge stops the flow before any click is attempted', async () => {
  const page = await open(profile({ extra: '<p>Please verify you are human</p>' }));
  const result = await processOutreachContact(page, root, 'Asha Kapoor', page.url(), 'Hi Asha', 'test-captcha');
  assert.equal(result.status, 'BLOCKED_SECURITY');
  await page.close();
});

test('LinkedIn requiring an email for an out-of-network invite is reported, not guessed', async () => {
  const html = profile().replace('<h2>Add a note to your invitation?</h2>', '<h2>Add a note to your invitation?</h2><p>To connect with Asha, please share their email address.</p>');
  const page = await open(html);
  const result = await processOutreachContact(page, root, 'Asha Kapoor', page.url(), 'Hi Asha', 'test-email');
  assert.equal(result.status, 'NEEDS_USER_INPUT');
  assert.match(result.reason!, /email/);
  assert.equal(await page.locator('#note-textarea').isVisible(), false);
  await page.close();
});

function seedOutreach(db: ReturnType<typeof openDatabase>, rows: Array<{ name: string; profileUrl: string }>): void {
  db.prepare("INSERT INTO jobs(company,title,location,job_url,source,detected_at,description,fit_score,status) VALUES('Acme','QA Engineer','Remote','https://example.com/job','fixture',?,'QA Engineer',90,'APPLICATION_READY')").run(new Date().toISOString());
  for (const [index, row] of rows.entries()) {
    db.prepare('INSERT INTO contacts(company,name,title,linkedin_url,relevance_score) VALUES (?,?,?,?,?)').run('Acme', row.name, 'Recruiter', row.profileUrl, 90 - index);
    const contactId = (db.prepare('SELECT id FROM contacts WHERE linkedin_url=?').get(row.profileUrl) as { id: number }).id;
    db.prepare("INSERT INTO outreach(job_id,contact_id,type,message,character_count,status,created_at) VALUES (1,?,?,?,?,?,?)").run(contactId, 'CONNECTION', `Hi ${row.name.split(' ')[0]}`, 10, 'OUTREACH_READY', new Date().toISOString());
  }
}

test('sendPreparedOutreach only marks SENT after explicit human confirmation, and leaves declines resumable', async () => {
  const dbRoot = await mkdtemp(join(tmpdir(), 'job-link-outreach-orch-'));
  const db = openDatabase(dbRoot);
  try {
    seedOutreach(db, [{ name: 'Asha Kapoor', profileUrl: 'https://www.linkedin.com/in/asha-confirmed' }, { name: 'Asha Kapoor', profileUrl: 'https://www.linkedin.com/in/asha-declined' }]);
    const context = await browser.newContext();
    await context.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: profile() }));
    let call = 0;
    const result = await sendPreparedOutreach(dbRoot, db, 'test-run', context, async () => { call++; return call === 1; });
    assert.equal(result.attempted, 2); assert.equal(result.readyToSend, 2); assert.equal(result.sent, 1);
    const rows = db.prepare('SELECT linkedin_url url, status, sent_at FROM outreach o JOIN contacts c ON c.id=o.contact_id ORDER BY c.linkedin_url').all() as Array<{ url: string; status: string; sent_at: string | null }>;
    const confirmed = rows.find((row) => row.url.includes('confirmed'))!; const declined = rows.find((row) => row.url.includes('declined'))!;
    assert.equal(confirmed.status, 'SENT'); assert.ok(confirmed.sent_at);
    assert.equal(declined.status, 'READY_TO_SEND'); assert.equal(declined.sent_at, null);
    await context.close();
  } finally { db.close(); await rm(dbRoot, { recursive: true, force: true }); }
});

test('sendPreparedOutreach stops the whole batch on a LinkedIn security or login stop, leaving later contacts untouched', async () => {
  const dbRoot = await mkdtemp(join(tmpdir(), 'job-link-outreach-blocked-'));
  const db = openDatabase(dbRoot);
  try {
    seedOutreach(db, [{ name: 'Asha Kapoor', profileUrl: 'https://www.linkedin.com/in/asha-blocked' }, { name: 'Asha Kapoor', profileUrl: 'https://www.linkedin.com/in/asha-untouched' }]);
    const context = await browser.newContext();
    await context.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: profile({ extra: '<label>Password<input type="password"></label>' }) }));
    const result = await sendPreparedOutreach(dbRoot, db, 'test-run', context, async () => true);
    assert.equal(result.attempted, 1); assert.equal(result.blocked, 1);
    const rows = db.prepare('SELECT linkedin_url url, status FROM outreach o JOIN contacts c ON c.id=o.contact_id ORDER BY c.linkedin_url').all() as Array<{ url: string; status: string }>;
    assert.equal(rows.find((row) => row.url.includes('blocked'))!.status, 'LOGIN_REQUIRED');
    assert.equal(rows.find((row) => row.url.includes('untouched'))!.status, 'OUTREACH_READY');
    await context.close();
  } finally { db.close(); await rm(dbRoot, { recursive: true, force: true }); }
});
