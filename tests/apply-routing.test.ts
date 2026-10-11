import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium, type Browser } from 'playwright';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/database.js';
import { processEligibleJobs } from '../src/pipeline.js';
import { processApplicationQueue, holdManualApplication } from '../src/application-runner.js';

let browser: Browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

const config = await loadConfig(process.cwd());
type Db = ReturnType<typeof openDatabase>;
async function env() { const root = await mkdtemp(join(tmpdir(), 'job-link-routing-')); await mkdir(join(root, 'resumes')); await writeFile(join(root, 'resumes', 'Main.pdf'), '%PDF-1.4\nControlled resume\n'); return { root, db: openDatabase(root) }; }
function seedRun(db: Db) { db.prepare("INSERT INTO runs(id,started_at,status,mode) VALUES('r',?,'STARTED','live')").run(new Date().toISOString()); db.prepare("INSERT INTO resumes(filename,category,keywords,updated_at,available) VALUES('Main.pdf','MAIN','[]',?,1)").run(new Date().toISOString()); }
function seedJob(db: Db, o: { url: string; app: string; source?: string; externalId?: string; metadata?: object; status?: string; title?: string }) {
  return Number(db.prepare(`INSERT INTO jobs(company,title,location,job_url,source,posted_at,detected_at,description,application_url,external_id,metadata,fit_score,status,evidence_mode,updated_at)
    VALUES('Acme',?,'Hyderabad',?,?,?,?,'QA role',?,?,?,90,?, 'live',?)`).run(o.title ?? 'Quality Engineer', o.url, o.source ?? 'linkedin', new Date(Date.now() - 3600_000).toISOString(), new Date().toISOString(), o.app, o.externalId ?? null, JSON.stringify(o.metadata ?? {}), o.status ?? 'REVIEW', new Date().toISOString()).lastInsertRowid);
}

test('LinkedIn postings that apply elsewhere with no employer link become manual-apply tasks; employer-hosted and unknown-kind ones queue', async () => {
  const { root, db } = await env();
  try {
    seedRun(db);
    const easy = seedJob(db, { url: 'https://www.linkedin.com/jobs/view/1', app: 'https://www.linkedin.com/jobs/view/1', metadata: { applyKind: 'external' } });
    const external = seedJob(db, { url: 'https://www.linkedin.com/jobs/view/2', app: 'https://boards.greenhouse.io/acme/jobs/9', metadata: { applyKind: 'external' }, title: 'QA Automation Engineer' });
    // Live 2026-09-30: discovery could not read the apply kind of 10 LinkedIn jobs (the button had not rendered); the run opens them once.
    const unknown = seedJob(db, { url: 'https://www.linkedin.com/jobs/view/3', app: 'https://www.linkedin.com/jobs/view/3', title: 'Senior Quality Engineer' });
    const result = await processEligibleJobs(root, db, config, 'r');
    assert.equal(result.queued, 2); assert.equal(result.needsUserInput, 1);
    assert.equal((db.prepare('SELECT status FROM applications WHERE job_id=?').get(unknown) as { status: string }).status, 'READY_FOR_REVIEW');
    const manual = db.prepare('SELECT status, ats, resume, last_error FROM applications WHERE job_id=?').get(easy) as { status: string; ats: string; resume: string; last_error: string };
    assert.equal(manual.status, 'NEEDS_USER_INPUT'); assert.equal(manual.ats, 'linkedin'); assert.equal(manual.resume, 'Main.pdf'); assert.match(manual.last_error, /Apply manually on LinkedIn.*Main\.pdf/);
    assert.equal((db.prepare('SELECT status FROM jobs WHERE id=?').get(easy) as { status: string }).status, 'NEEDS_USER_INPUT');
    assert.equal((db.prepare("SELECT COUNT(*) n FROM questions WHERE job_id=? AND category='MANUAL_APPLY' AND status='OPEN'").get(easy) as { n: number }).n, 1);
    assert.equal((db.prepare('SELECT status FROM applications WHERE job_id=?').get(external) as { status: string }).status, 'READY_FOR_REVIEW');
    // Running again must not duplicate the manual task or re-queue anything.
    const again = await processEligibleJobs(root, db, config, 'r');
    assert.equal(again.queued, 0); assert.equal((db.prepare('SELECT COUNT(*) n FROM applications').get() as { n: number }).n, 3);
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

const leverForm = '<!doctype html><html><head><title>Acme Careers</title></head><body><h1>Quality Engineer</h1><h2>Acme</h2><form id="application-form"><label>Full name<input name="name" required></label><label>Email<input name="email" type="email" required></label><label>Resume<input type="file" name="resume" accept="application/pdf" required></label><button type="submit">Submit application</button></form></body></html>';
async function queueAndRun(metadata: object, page: string = leverForm) {
  const { root, db } = await env();
  try {
    seedRun(db);
    const id = seedJob(db, { url: 'https://www.linkedin.com/jobs/view/4468239318', app: 'https://jobs.lever.co/acme/abc-123/apply', externalId: '4468239318', metadata });
    await processEligibleJobs(root, db, config, 'r');
    const context = await browser.newContext();
    await context.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: page }));
    const cfg = structuredClone(config); cfg.application.delay_between_jobs_ms = 0;
    await processApplicationQueue(root, db, cfg, 'r', async () => context);
    await context.close();
    return { ...(db.prepare('SELECT status, last_error FROM applications WHERE job_id=?').get(id) as { status: string; last_error: string | null }), categories: (db.prepare("SELECT category FROM questions WHERE job_id=? AND status='OPEN'").all(id) as Array<{ category: string }>).map((q) => q.category) };
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
}

test('a LinkedIn-sourced external apply is filled and left ready (never submitted) because the employer page is verified by heading and company', async () => {
  const row = await queueAndRun({ applyKind: 'external' });
  assert.equal(row.status, 'READY_FOR_SUBMISSION', row.last_error ?? '');
});

test('without the external-apply marker the LinkedIn numeric id is required in the URL, so the same page is refused rather than filled', async () => {
  const row = await queueAndRun({});
  assert.equal(row.status, 'FAILED');
  assert.match(row.last_error ?? '', /identity/i);
});

// The employer's own site duplicates this control's accessible name directly in the aria-label attribute ("EmailEmail"), with no
// wrapping <label> element at all — a distinct duplication path from a <label>'s cloned/joined text, which read separately.
const rawAriaLabelForm = '<!doctype html><html><head><title>Acme Careers</title></head><body><h1>Quality Engineer</h1><h2>Acme</h2><form id="application-form"><input name="name" aria-label="Full nameFull name" required><input name="email" type="email" aria-label="EmailEmail" required><label>Resume<input type="file" name="resume" accept="application/pdf" required></label><button type="submit">Submit application</button></form></body></html>';

test('a control whose accessible name is duplicated in its own aria-label attribute ("EmailEmail") is still recognised, not left as an unanswerable question', async () => {
  const row = await queueAndRun({ applyKind: 'external' }, rawAriaLabelForm);
  assert.equal(row.status, 'READY_FOR_SUBMISSION', row.last_error ?? '');
  assert.deepEqual(row.categories, []);
});

test('when the browser dies mid-attempt, a reopen callback gets a fresh one and the same job is retried once, instead of the whole queue ending on one crash', async () => {
  const { root, db } = await env();
  try {
    seedRun(db);
    const id = seedJob(db, { url: 'https://www.linkedin.com/jobs/view/4468239318', app: 'https://jobs.lever.co/acme/abc-123/apply', externalId: '4468239318', metadata: { applyKind: 'external' } });
    await processEligibleJobs(root, db, config, 'r');
    let reopened = 0;
    const dead = { newPage: async () => { throw new Error('Target page, context or browser has been closed'); } } as unknown as import('playwright').BrowserContext;
    const working = await browser.newContext();
    await working.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: leverForm }));
    // getContext and reopen must share state, exactly as the real CLI wires them (openPersistentSession result cached in an
    // outer variable): a reopen that doesn't change what getContext next returns wouldn't really test anything.
    let current = dead;
    const cfg = structuredClone(config); cfg.application.delay_between_jobs_ms = 0;
    const result = await processApplicationQueue(root, db, cfg, 'r', async () => current, undefined, false, async () => { reopened++; current = working; return current; });
    assert.equal(result.processed, 1); assert.equal(reopened, 1);
    const row = db.prepare('SELECT status, last_error FROM applications WHERE job_id=?').get(id) as { status: string; last_error: string | null };
    assert.equal(row.status, 'READY_FOR_SUBMISSION', row.last_error ?? '');
    await working.close();
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

test('when the retry also fails, the real second reason is reported and the job goes to a genuine FAILED (not a silent loop)', async () => {
  const { root, db } = await env();
  try {
    seedRun(db);
    const id = seedJob(db, { url: 'https://www.linkedin.com/jobs/view/4468239319', app: 'https://jobs.lever.co/acme/xyz-999/apply', externalId: '4468239319', metadata: { applyKind: 'external' } });
    await processEligibleJobs(root, db, config, 'r');
    let calls = 0;
    const dead = { newPage: async () => { calls++; throw new Error('Target page, context or browser has been closed'); } } as unknown as import('playwright').BrowserContext;
    const cfg = structuredClone(config); cfg.application.delay_between_jobs_ms = 0;
    const result = await processApplicationQueue(root, db, cfg, 'r', async () => dead, undefined, false, async () => dead);
    assert.equal(result.processed, 1); assert.equal(calls, 2, 'one initial attempt and exactly one retry, not a loop');
    const row = db.prepare('SELECT status, last_error FROM applications WHERE job_id=?').get(id) as { status: string; last_error: string | null };
    assert.equal(row.status, 'RETRYABLE_TECHNICAL_FAILURE'); assert.match(row.last_error ?? '', /Target page, context or browser has been closed/);
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

// Most of the time a browser crash is never thrown out to processApplicationQueue at all: processApplicationPage has its own
// catch and turns it into a normal FAILED result. Deterministically reproduces that shape (not the direct-throw shape above) by
// closing the real context right after page.goto() resolves, so every later locator call inside processApplicationPage — not
// the goto itself — is what actually hits the closed context, exactly like the real job that first exposed this.
async function contextThatDiesAfterLoad(): Promise<import('playwright').BrowserContext> {
  const context = await browser.newContext();
  await context.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: leverForm }));
  const realNewPage = context.newPage.bind(context);
  (context as unknown as { newPage: () => Promise<import('playwright').Page> }).newPage = async () => {
    const real = await realNewPage();
    return new Proxy(real, { get(target, prop, receiver) {
      if (prop !== 'goto') return Reflect.get(target, prop, receiver);
      return async (...args: unknown[]) => {
        const response = await (target.goto as (...a: unknown[]) => Promise<unknown>)(...args);
        await context.close().catch(() => undefined);
        return response;
      };
    } });
  };
  return context;
}

test('a crash that processApplicationPage absorbs internally (a locator call hits the closed context, not page.goto itself) is still recognised and retried', async () => {
  const { root, db } = await env();
  try {
    seedRun(db);
    const id = seedJob(db, { url: 'https://www.linkedin.com/jobs/view/4468239321', app: 'https://jobs.lever.co/acme/dies-after-load/apply', externalId: '4468239321', metadata: { applyKind: 'external' } });
    await processEligibleJobs(root, db, config, 'r');
    let reopened = 0;
    let current = await contextThatDiesAfterLoad();
    const cfg = structuredClone(config); cfg.application.delay_between_jobs_ms = 0;
    const result = await processApplicationQueue(root, db, cfg, 'r', async () => current, undefined, false, async () => { reopened++; current = await browser.newContext(); await current.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: leverForm })); return current; });
    assert.equal(result.processed, 1); assert.equal(reopened, 1);
    const row = db.prepare('SELECT status, last_error FROM applications WHERE job_id=?').get(id) as { status: string; last_error: string | null };
    assert.equal(row.status, 'READY_FOR_SUBMISSION', row.last_error ?? '');
    await current.close().catch(() => undefined);
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

test('without a reopen callback (the daily cloud job has none), a crash fails that one job exactly as before, no retry attempted', async () => {
  const { root, db } = await env();
  try {
    seedRun(db);
    const id = seedJob(db, { url: 'https://www.linkedin.com/jobs/view/4468239320', app: 'https://jobs.lever.co/acme/no-retry/apply', externalId: '4468239320', metadata: { applyKind: 'external' } });
    await processEligibleJobs(root, db, config, 'r');
    let calls = 0;
    const dead = { newPage: async () => { calls++; throw new Error('Target page, context or browser has been closed'); } } as unknown as import('playwright').BrowserContext;
    const cfg = structuredClone(config); cfg.application.delay_between_jobs_ms = 0;
    await processApplicationQueue(root, db, cfg, 'r', async () => dead);
    assert.equal(calls, 1);
    assert.equal((db.prepare('SELECT status FROM applications WHERE job_id=?').get(id) as { status: string }).status, 'RETRYABLE_TECHNICAL_FAILURE');
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

const roleWithoutForm = '<!doctype html><html><head><title>Acme Careers</title></head><body><h1>Quality Engineer</h1><h2>Acme</h2><p>Join our team in Hyderabad.</p><a href="#apply">Apply now</a></body></html>';
test('an employer page without a supported form is unsupported, not a candidate question', async () => {
  const row = await queueAndRun({ applyKind: 'external' }, roleWithoutForm);
  assert.equal(row.status, 'UNSUPPORTED_ATS', row.last_error ?? '');
  assert.match(row.last_error ?? '', /No supported application form/);
  assert.deepEqual(row.categories, []);
});

test('a page saying the posting is closed is skipped, not a manual task', async () => {
  const row = await queueAndRun({ applyKind: 'external' }, '<!doctype html><html><body><h1>Quality Engineer</h1><h2>Acme</h2><p>Sorry, this position is no longer accepting applications.</p></body></html>');
  assert.equal(row.status, 'CLOSED_JOB'); assert.match(row.last_error ?? '', /closed or inactive/); assert.deepEqual(row.categories, []);
});


test('manual handoff leaves the page and typed phone untouched until the person closes the tab',async()=>{
  const page=await browser.newPage();await page.setContent('<input id="phone" type="tel">');
  let finished=false;const hold=holdManualApplication(page,'NEEDS_USER_INPUT',true).then(()=>{finished=true});
  await page.locator('#phone').fill('1234567890');
  assert.equal(await page.locator('#phone').inputValue(),'1234567890');
  assert.equal(finished,false);
  await page.close();await hold;assert.equal(finished,true);
});
