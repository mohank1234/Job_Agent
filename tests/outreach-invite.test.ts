import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium, type Browser } from 'playwright';
import { processOutreachContact, connectTimings, inviteUrlFor, sendOpenInvitation } from '../src/linkedin-connect.js';
import { sendPreparedOutreach } from '../src/outreach-send.js';
import { applyLedger, readLedger, recordOutreach } from '../src/outreach-ledger.js';
import { openDatabase } from '../src/database.js';

let browser: Browser;
let root: string;
before(async () => { connectTimings.inviteDialogMs = 400; root = await mkdtemp(join(tmpdir(), 'job-link-invite-')); browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); await rm(root, { recursive: true, force: true }); });

// LinkedIn's real invitation dialog opens from its own address (structure captured from a live page); its Send button stays disabled until a note is typed.
const inviteDialog = (person: string): string => `<!doctype html><html><head><title>LinkedIn</title></head><body>
<div role="dialog" id="dlg"><h2>Add a note to your invitation?</h2><p>Personalize your invitation to <strong>${person}</strong> by adding a note.</p>
  <button type="button" aria-label="Add a note" id="add">Add a note</button><button type="button" aria-label="Send without a note" id="without">Send without a note</button>
  <div id="area" style="display:none"><textarea id="note"></textarea><span id="count">0/300</span><button type="button" aria-label="Cancel adding a note">Cancel</button><button type="button" id="send" aria-label="Send invitation" disabled>Send</button></div>
</div>
<script>
document.getElementById('add').addEventListener('click', function () { document.getElementById('area').style.display = 'block'; document.getElementById('add').style.display = 'none'; document.getElementById('without').style.display = 'none'; });
document.getElementById('note').addEventListener('input', function () { document.getElementById('send').disabled = !document.getElementById('note').value; document.getElementById('count').textContent = document.getElementById('note').value.length + '/300'; });
document.getElementById('send').addEventListener('click', function () { fetch('/__sent?to=' + encodeURIComponent(location.search)).catch(function () {}); document.getElementById('dlg').remove(); });
</script></body></html>`;
const plainPage = '<!doctype html><html><head><title>LinkedIn</title></head><body><h1>Someone</h1></body></html>';

// dialogOnce: LinkedIn offers the dialog the first time; afterwards that person's profile shows "Pending" (the invitation went out some other way).
const pendingProfile = '<!doctype html><html><head><title>LinkedIn</title></head><body><h1>Asha Kapoor</h1><button type="button">Pending</button></body></html>';
async function inviteContext(people: Record<string, string>, options: { dialogOnce?: boolean } = {}) {
  const sent: string[] = [];
  const served = new Set<string>();
  const context = await browser.newContext();
  await context.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === '/__sent') { sent.push(decodeURIComponent(url.searchParams.get('to') ?? '')); return route.fulfill({ status: 200, body: 'ok' }); }
    const vanity = url.searchParams.get('vanityName');
    if (vanity && people[vanity] && !(options.dialogOnce && served.has(vanity))) { served.add(vanity); return route.fulfill({ contentType: 'text/html', body: inviteDialog(people[vanity]) }); }
    return route.fulfill({ contentType: 'text/html', body: options.dialogOnce && (vanity || url.pathname.startsWith('/in/')) ? pendingProfile : plainPage });
  });
  return { context, sent };
}

function seedOutreach(db: ReturnType<typeof openDatabase>, urls: string[]): void {
  db.prepare("INSERT INTO jobs(company,title,location,job_url,source,detected_at,description,fit_score,status) VALUES('Acme','QA Engineer','Remote','https://example.com/job','fixture',?,'QA Engineer',90,'APPLICATION_READY')").run(new Date().toISOString());
  for (const [index, profileUrl] of urls.entries()) {
    db.prepare('INSERT INTO contacts(company,name,title,linkedin_url,relevance_score) VALUES (?,?,?,?,?)').run('Acme', 'Asha Kapoor', 'Recruiter at Acme', profileUrl, 90 - index);
    const contactId = (db.prepare('SELECT id FROM contacts WHERE linkedin_url=?').get(profileUrl) as { id: number }).id;
    db.prepare("INSERT INTO outreach(job_id,contact_id,type,message,character_count,status,created_at) VALUES (1,?,?,?,?,?,?)").run(contactId, 'CONNECTION', 'Hi Asha', 7, 'OUTREACH_READY', new Date().toISOString());
  }
}

test('the invitation address is derived from a LinkedIn profile URL, and only from one', () => {
  assert.equal(inviteUrlFor('https://linkedin.com/in/naveenpalavalasa'), 'https://www.linkedin.com/preload/search-custom-invite/?vanityName=naveenpalavalasa');
  assert.equal(inviteUrlFor('https://www.linkedin.com/in/asha-kapoor/?miniProfileUrn=x'), 'https://www.linkedin.com/preload/search-custom-invite/?vanityName=asha-kapoor');
  assert.equal(inviteUrlFor('https://example.com/in/asha'), undefined);
});

test('the invitation dialog is opened directly, checked against the contact, filled with the note, and left open without sending', async () => {
  const { context, sent } = await inviteContext({ 'asha-kapoor': 'Asha Kapoor' });
  const page = await context.newPage();
  const note = "Hi Asha, I'm a QA engineer with 5 years of experience in Selenium and Playwright automation. I'd appreciate connecting. Test Candidate";
  const result = await processOutreachContact(page, root, 'Asha Kapoor', 'https://www.linkedin.com/in/asha-kapoor', note, 'invite-happy');
  assert.equal(result.status, 'READY_TO_SEND', result.reason);
  assert.equal(await page.getByRole('dialog').getByRole('textbox').inputValue(), note);
  assert.equal(sent.length, 0, 'filling the note must never send it');
  await context.close();
});

test('an invitation dialog for a different person than the contact is refused and nothing is typed into it', async () => {
  const { context, sent } = await inviteContext({ 'asha-kapoor': 'Someone Else' });
  const page = await context.newPage();
  const result = await processOutreachContact(page, root, 'Asha Kapoor', 'https://www.linkedin.com/in/asha-kapoor', 'Hi', 'invite-wrong-person');
  assert.equal(result.status, 'NEEDS_USER_INPUT'); assert.match(result.reason ?? '', /not for Asha Kapoor/);
  assert.equal(await page.getByRole('textbox').count(), 0); assert.equal(sent.length, 0);
  await context.close();
});

test('sendOpenInvitation clicks Send only when asked, once, and reports it sent when the dialog closes', async () => {
  const { context, sent } = await inviteContext({ 'asha-kapoor': 'Asha Kapoor' });
  const page = await context.newPage();
  const result = await processOutreachContact(page, root, 'Asha Kapoor', 'https://www.linkedin.com/in/asha-kapoor', 'Hi Asha, nice to connect.', 'invite-send');
  assert.equal(result.status, 'READY_TO_SEND'); assert.equal(sent.length, 0);
  const outcome = await sendOpenInvitation(page);
  assert.equal(outcome.sent, true, outcome.reason); assert.equal(sent.length, 1); assert.match(sent[0], /asha-kapoor/);
  await context.close();
});

test('with clickSend the approved note is sent, "never" drops that person for good, "stop" ends the batch, and dry run sends nothing', async () => {
  const dbRoot = await mkdtemp(join(tmpdir(), 'job-link-outreach-click-'));
  const db = openDatabase(dbRoot);
  try {
    const letters = ['a', 'b', 'c', 'd'];
    seedOutreach(db, letters.map((letter) => `https://www.linkedin.com/in/person-${letter}`));
    const { context, sent } = await inviteContext(Object.fromEntries(letters.map((letter) => [`person-${letter}`, 'Asha Kapoor'])));
    const dry = await sendPreparedOutreach(dbRoot, db, 'r', context, async () => { throw new Error('a dry run must not ask'); }, 50, undefined, { clickSend: true, dryRun: true });
    assert.equal(dry.readyToSend, 4); assert.equal(sent.length, 0);
    assert.equal((db.prepare("SELECT COUNT(*) n FROM outreach WHERE status='OUTREACH_READY'").get() as { n: number }).n, 4);
    const decisions: Array<boolean | 'skip' | 'stop'> = [true, 'skip', 'stop'];
    const prompts: string[] = [];
    const result = await sendPreparedOutreach(dbRoot, db, 'r', context, async (prompt) => { prompts.push(prompt); return decisions.shift()!; }, 50, undefined, { clickSend: true });
    assert.equal(result.attempted, 3); assert.equal(result.sent, 1); assert.equal(result.declined, 1);
    assert.equal(sent.length, 1); assert.match(sent[0], /person-a/);
    assert.match(prompts[0], /Send this invitation to Asha Kapoor/); assert.match(prompts[0], /"Hi Asha"/);
    const status = (letter: string) => (db.prepare('SELECT o.status FROM outreach o JOIN contacts c ON c.id=o.contact_id WHERE c.linkedin_url LIKE ?').get(`%person-${letter}`) as { status: string }).status;
    assert.deepEqual(letters.map(status), ['SENT', 'DECLINED', 'OUTREACH_READY', 'OUTREACH_READY']);
    await context.close();
  } finally { db.close(); await rm(dbRoot, { recursive: true, force: true }); }
});

async function withDb<T>(run: (dbRoot: string, db: ReturnType<typeof openDatabase>) => Promise<T>): Promise<T> {
  const dbRoot = await mkdtemp(join(tmpdir(), 'job-link-ledger-'));
  const db = openDatabase(dbRoot);
  try { return await run(dbRoot, db); } finally { db.close(); await rm(dbRoot, { recursive: true, force: true }); }
}
const statusOf = (db: ReturnType<typeof openDatabase>, letter: string) => (db.prepare('SELECT o.status FROM outreach o JOIN contacts c ON c.id=o.contact_id WHERE c.linkedin_url LIKE ?').get(`%person-${letter}`) as { status: string } | undefined)?.status;
const ledgerEntry = (letter: string, status: 'SENT' | 'PENDING' | 'DECLINED' = 'SENT') => ({ at: '2026-09-21T04:55:17.000Z', status, url: `https://linkedin.com/in/person-${letter}`, name: 'Asha Kapoor', title: 'Recruiter at Acme', company: 'Acme', message: 'Hi Asha', jobId: 1 });

test('sent invitations are recorded outside the database and re-applied to a database restored from an older copy', async () => {
  await withDb(async (dbRoot, db) => {
    seedOutreach(db, ['a', 'b'].map((letter) => `https://linkedin.com/in/person-${letter}`));
    await recordOutreach(dbRoot, ledgerEntry('a')); await recordOutreach(dbRoot, ledgerEntry('c')); // c is not in this database at all (it came from a database that was replaced)
    await import('node:fs/promises').then((fs) => fs.appendFile(join(dbRoot, 'data', 'outreach-ledger.jsonl'), '{"torn line'));
    assert.equal((await readLedger(dbRoot)).length, 2, 'a torn last line is ignored');
    assert.deepEqual(applyLedger(db, await readLedger(dbRoot)), { updated: 1, inserted: 1 });
    assert.deepEqual(['a', 'b', 'c'].map((letter) => statusOf(db, letter)), ['SENT', 'OUTREACH_READY', 'SENT']);
    assert.deepEqual(applyLedger(db, await readLedger(dbRoot)), { updated: 0, inserted: 0 }, 'applying twice changes nothing');
    await recordOutreach(dbRoot, ledgerEntry('b', 'DECLINED')); await recordOutreach(dbRoot, ledgerEntry('a', 'PENDING'));
    applyLedger(db, await readLedger(dbRoot));
    assert.deepEqual(['a', 'b'].map((letter) => statusOf(db, letter)), ['SENT', 'DECLINED'], 'a known outcome is never overwritten');
  });
});

test('after a restore, people already invited are not invited again and only the rest are sent', async () => {
  await withDb(async (dbRoot, db) => {
    seedOutreach(db, ['a', 'b'].map((letter) => `https://linkedin.com/in/person-${letter}`)); // every row is back to OUTREACH_READY, as after the restore
    await recordOutreach(dbRoot, ledgerEntry('a'));
    const { context, sent } = await inviteContext({ 'person-a': 'Asha Kapoor', 'person-b': 'Asha Kapoor' });
    const result = await sendPreparedOutreach(dbRoot, db, 'r', context, async () => true, 50, undefined, { clickSend: true });
    assert.equal(result.attempted, 1); assert.equal(result.sent, 1);
    assert.equal(sent.length, 1); assert.match(sent[0], /person-b/);
    assert.deepEqual(['a', 'b'].map((letter) => statusOf(db, letter)), ['SENT', 'SENT']);
    assert.equal((await readLedger(dbRoot)).filter((entry) => entry.url.endsWith('person-b')).length, 1, 'the new send is recorded too');
    await context.close();
  });
});

test('a closed or crashed browser is replaced and the batch carries on, instead of ending with an error', async () => {
  await withDb(async (dbRoot, db) => {
    seedOutreach(db, ['a', 'b'].map((letter) => `https://linkedin.com/in/person-${letter}`));
    const { context: live, sent } = await inviteContext({ 'person-a': 'Asha Kapoor', 'person-b': 'Asha Kapoor' });
    // First a browser that is already gone when a page is asked for, then one that dies just after handing out a page.
    const dead = { newPage: async () => { throw new Error('browserContext.newPage: Target page, context or browser has been closed'); } } as unknown as import('playwright').BrowserContext;
    let reopened = 0;
    const dyingBrowser = await browser.newContext();
    const dying = { newPage: async () => { const page = await dyingBrowser.newPage(); await dyingBrowser.close(); return page; } } as unknown as import('playwright').BrowserContext;
    const contexts = [dying, live];
    const result = await sendPreparedOutreach(dbRoot, db, 'r', dead, async () => true, 50, undefined, { clickSend: true, reopen: async () => { reopened++; return contexts.shift()!; } });
    assert.equal(result.attempted, 2); assert.equal(result.sent, 2, JSON.stringify(result));
    assert.ok(reopened >= 2, 'a fresh browser was opened for the dead one and again for the one that crashed mid-contact');
    assert.equal(sent.length, 2);
    await live.close();
  });
});

test('if the invitation dialog has closed by the time you approve, the same note is reopened once and sent', async () => {
  await withDb(async (dbRoot, db) => {
    seedOutreach(db, ['a'].map((letter) => `https://linkedin.com/in/person-${letter}`));
    const { context, sent } = await inviteContext({ 'person-a': 'Asha Kapoor' });
    const result = await sendPreparedOutreach(dbRoot, db, 'r', context, async () => {
      for (const page of context.pages()) await page.evaluate(() => document.getElementById('dlg')?.remove()); // it closed while the note waited for the approval
      return true;
    }, 50, undefined, { clickSend: true });
    assert.equal(result.sent, 1, JSON.stringify(result)); assert.equal(sent.length, 1);
    assert.equal(statusOf(db, 'a'), 'SENT');
    await context.close();
  });
});

test('if the dialog closed because the invitation is already pending, nothing more is sent and that is recorded', async () => {
  await withDb(async (dbRoot, db) => {
    seedOutreach(db, ['a'].map((letter) => `https://linkedin.com/in/person-${letter}`));
    const { context, sent } = await inviteContext({ 'person-a': 'Asha Kapoor' }, { dialogOnce: true });
    const result = await sendPreparedOutreach(dbRoot, db, 'r', context, async () => {
      for (const page of context.pages()) await page.evaluate(() => document.getElementById('dlg')?.remove());
      return true;
    }, 50, undefined, { clickSend: true });
    assert.equal(result.sent, 0); assert.equal(result.pending, 1); assert.equal(sent.length, 0);
    assert.equal(statusOf(db, 'a'), 'PENDING');
    assert.equal((await readLedger(dbRoot))[0]?.status, 'PENDING');
    await context.close();
  });
});
