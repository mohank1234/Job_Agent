import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium, type Browser } from 'playwright';
import { openDatabase } from '../src/database.js';
import { LinkedInBudget } from '../src/linkedin-budget.js';
import { hydrateJob, unwrapRedirect } from '../src/sources/browser.js';
import { LinkedInJobSource, linkedinJobKey } from '../src/sources/linkedin.js';
import { sourceSchema } from '../src/sources/settings.js';
import { loadConfig } from '../src/config.js';
import type { Job } from '../src/core/types.js';

let browser: Browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

async function withDb<T>(run: (db: ReturnType<typeof openDatabase>, root: string) => Promise<T> | T): Promise<T> {
  const root = await mkdtemp(join(tmpdir(), 'job-link-budget-'));
  const db = openDatabase(root);
  try { return await run(db, root); } finally { db.close(); await rm(root, { recursive: true, force: true }); }
}

test('the daily page budget is enforced across every kind of LinkedIn page and resets on a new UTC day', async () => {
  await withDb((db) => {
    let now = new Date('2026-09-20T05:00:00Z');
    const budget = new LinkedInBudget(db, { linkedin_daily_page_limit: 3, linkedin_block_cooldown_hours: 72 }, () => now);
    assert.equal(budget.allow('job-search'), true); assert.equal(budget.allow('job-detail'), true); assert.equal(budget.allow('people-search'), true);
    assert.equal(budget.allow('profile'), false); assert.equal(budget.used(), 3); assert.equal(budget.status(), 'EXHAUSTED');
    now = new Date('2026-09-21T00:00:01Z');
    assert.equal(budget.remaining(), 3); assert.equal(budget.allow('job-search'), true);
  });
});

test('a login or security block pauses LinkedIn for the cooldown, records nothing while paused, and can be cleared', async () => {
  await withDb((db) => {
    let now = new Date('2026-09-20T05:00:00Z');
    const budget = new LinkedInBudget(db, { linkedin_daily_page_limit: 50, linkedin_block_cooldown_hours: 72 }, () => now);
    const until = budget.recordBlock('authwall');
    assert.equal(until.toISOString(), '2026-09-23T05:00:00.000Z');
    assert.equal(budget.status(), 'BLOCKED'); assert.equal(budget.allow('job-search'), false); assert.equal(budget.used(), 0);
    assert.match(budget.describe(), /paused until 2026-09-23/);
    now = new Date('2026-09-23T05:00:01Z');
    assert.equal(budget.status(), 'OK'); assert.equal(budget.allow('job-search'), true);
    budget.recordBlock('again'); budget.clearBlock();
    assert.equal(budget.status(), 'OK');
  });
});

test('a daily limit of 0 means no daily cap, while a block still pauses LinkedIn', async () => {
  await withDb((db) => {
    const budget = new LinkedInBudget(db, { linkedin_daily_page_limit: 0, linkedin_block_cooldown_hours: 1 });
    for (let page = 0; page < 500; page++) assert.equal(budget.allow('job-search'), true);
    assert.equal(budget.status(), 'OK'); assert.equal(budget.remaining(), Number.POSITIVE_INFINITY); assert.equal(budget.unlimited(), true);
    assert.match(budget.describe(), /500 \(no daily limit\)/);
    budget.recordBlock('429');
    assert.equal(budget.status(), 'BLOCKED'); assert.equal(budget.allow('job-search'), false);
  });
});

test('LinkedIn redirect wrappers are unwrapped to the real employer URL, using hrefs captured from live job pages', () => {
  const base = 'https://www.linkedin.com/jobs/view/4466818761';
  assert.equal(unwrapRedirect('https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fsimcorp%2Ewd3%2Emyworkdayjobs%2Ecom%2Fen-US%2FSimCorp_Jobs%2Fjob%2FHyderabad%2FPrincipal-Site-Reliability-Engineer_R-210919-1%3Fsource%3DLinkedIn&urlhash=Mxzb&mt=8P68', base),
    'https://simcorp.wd3.myworkdayjobs.com/en-US/SimCorp_Jobs/job/Hyderabad/Principal-Site-Reliability-Engineer_R-210919-1?source=LinkedIn');
  assert.equal(unwrapRedirect('/jobs/view/externalApply/123?url=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F9', base), 'https://boards.greenhouse.io/acme/jobs/9');
  assert.equal(unwrapRedirect('https://boards.greenhouse.io/acme/jobs/9', base), 'https://boards.greenhouse.io/acme/jobs/9');
  assert.equal(unwrapRedirect('https://www.linkedin.com/jobs/view/1', base), 'https://www.linkedin.com/jobs/view/1');
  assert.equal(unwrapRedirect('https://www.linkedin.com/safety/go/?url=javascript%3Aalert(1)', base), 'https://www.linkedin.com/safety/go/?url=javascript%3Aalert(1)');
});

const job: Job = { company: 'Acme', title: 'QA Engineer', location: 'Hyderabad, India', workMode: 'On-site', jobUrl: 'https://www.linkedin.com/jobs/view/1', source: 'linkedin', postedAt: '', description: '' };
const detailConfig = { applicationLinkSelector: 'a[aria-label*="Apply on company website" i]', easyApplySelector: '[aria-label*="Easy Apply" i]' };
async function page(html: string) { const p = await browser.newPage(); await p.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: html })); await p.goto('https://www.linkedin.com/jobs/view/1'); return p; }

test('an external apply link is read by its accessible name and unwrapped; the job is flagged external', async () => {
  const p = await page('<html><body><a aria-label="Apply on company website" href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F123&urlhash=x">Apply</a></body></html>');
  const result = await hydrateJob(p, job, detailConfig);
  assert.equal(result.applicationUrl, 'https://boards.greenhouse.io/acme/jobs/123');
  assert.equal(result.metadata?.applyKind, 'external');
  await p.close();
});

test('an Easy Apply job keeps its LinkedIn URL and is flagged easy_apply so it is never sent to the form filler', async () => {
  const p = await page('<html><body><button aria-label="Easy Apply to QA Engineer at Acme">Easy Apply</button></body></html>');
  const result = await hydrateJob(p, job, detailConfig);
  assert.equal(result.applicationUrl, undefined);
  assert.equal(result.metadata?.applyKind, 'easy_apply');
  await p.close();
});

test('the LinkedIn source fetches a posting once across searches, skips detail pages for non-testing titles, and stops at the page budget', async () => {
  await withDb(async (db) => {
    const config = await loadConfig(process.cwd());
    const now = new Date().toISOString();
    const card = (id: number, title: string, ref: number) => `<li class="base-search-card"><a class="base-card__full-link" href="/jobs/view/${id}?eBP=NON_CHARGEABLE_CHANNEL&refId=search${ref}%3D%3D&trackingId=t${ref}"></a><h3 class="base-search-card__title">${title}</h3><h4 class="base-search-card__subtitle">Acme</h4><span class="job-search-card__location">Hyderabad, India</span><time datetime="${now}"></time></li>`;
    const searchHtml = (ref: number) => `<html><body><ul>${card(101, 'QA Engineer', ref)}${card(102, 'Product Manager', ref)}${card(103, 'SDET', ref)}</ul></body></html>`;
    const detailHtml = `<html><body><div>About the job\n${'Manual and automation testing with Selenium and API testing. '.repeat(4)}\nSet alert for similar jobs</div><a aria-label="Apply on company website" href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F7">Apply</a></body></html>`;
    const run = async (limit: number) => {
      const context = await browser.newContext(); const hits = { search: 0, detail: 0 };
      await context.route('**/*', (route) => { const path = new URL(route.request().url()).pathname; if (path.startsWith('/jobs/search')) hits.search++; else if (path.startsWith('/jobs/view')) hits.detail++; return route.fulfill({ contentType: 'text/html', body: path.startsWith('/jobs/view') ? detailHtml : searchHtml(hits.search) }); });
      const budget = new LinkedInBudget(db, { linkedin_daily_page_limit: limit, linkedin_block_cooldown_hours: 72 });
      const settings = sourceSchema.parse({ name: 'LinkedIn', type: 'linkedin', max_searches: 2, max_pages: 1 }) as Extract<ReturnType<typeof sourceSchema.parse>, { type: 'linkedin' }>;
      const source = new LinkedInJobSource(context, settings, tmpdir(), budget);
      const jobs = await source.discover(config);
      await context.close();
      return { jobs, hits, source, budget };
    };
    const first = await run(50);
    // 2 searches return the same 3 postings under different tracking URLs (as LinkedIn does): only the 2 testing titles get a detail page, once.
    assert.equal(first.hits.search, 2); assert.equal(first.hits.detail, 2);
    assert.equal(first.jobs.length, 3);
    assert.equal(first.jobs.filter((item) => item.metadata?.applyKind === 'external').length, 2);
    assert.ok(first.jobs.filter((item) => item.metadata?.applyKind === 'external').every((item) => item.applicationUrl === 'https://boards.greenhouse.io/acme/jobs/7'));
    assert.equal(first.budget.used(), 4);
    db.prepare('DELETE FROM linkedin_actions').run();
    const capped = await run(2); // 1 search page + 1 detail page, then the budget refuses the next reservation
    assert.equal(capped.budget.used(), 2); assert.equal(capped.source.truncated, true); assert.ok(capped.source.warnings.some((warning) => /budget/i.test(warning)));
    assert.ok(capped.hits.search + capped.hits.detail <= 2);
  });
});

test('a LinkedIn posting is identified by its numeric id, whatever tracking parameters or slug the URL carries', () => {
  const key = 'linkedin:4467328607';
  assert.equal(linkedinJobKey('https://www.linkedin.com/jobs/view/4467328607?eBP=NON_CHARGEABLE_CHANNEL&refId=abc%3D%3D&trackingId=x'), key);
  assert.equal(linkedinJobKey('https://in.linkedin.com/jobs/view/qa-automation-engineer-at-lumber-4467328607?position=3&pageNum=0'), key);
  assert.equal(linkedinJobKey('https://www.linkedin.com/jobs/view/4467328607/?refId=zzz'), key);
  assert.equal(linkedinJobKey('https://example.com/jobs/1?utm=x'), 'https://example.com/jobs/1');
});

test('a block seen from one environment does not pause another, while the daily page count stays shared', async () => {
  await withDb((db) => {
    const now = new Date('2026-09-20T05:00:00Z');
    const limits = { linkedin_daily_page_limit: 5, linkedin_block_cooldown_hours: 72 };
    const cloud = new LinkedInBudget(db, limits, () => now, 'ci'); const laptop = new LinkedInBudget(db, limits, () => now, 'local');
    cloud.recordBlock('HTTP 429');
    assert.equal(cloud.status(), 'BLOCKED'); assert.equal(laptop.status(), 'OK');
    assert.equal(laptop.allow('job-search'), true); assert.equal(cloud.used(), 1);
    // A pause recorded before pauses were scoped belongs to no environment: ignored, and removed by clearBlock.
    db.prepare("INSERT INTO linkedin_state(key,value) VALUES('blocked_until',?)").run('2026-12-31T00:00:00.000Z');
    assert.equal(laptop.status(), 'OK'); laptop.clearBlock();
    assert.equal((db.prepare("SELECT COUNT(*) n FROM linkedin_state WHERE key='blocked_until'").get() as { n: number }).n, 0);
    assert.equal(cloud.status(), 'BLOCKED');
  });
});

test('page loads are spaced by the minimum gap, never wait before the first, and a paused LinkedIn neither waits nor records', async () => {
  await withDb(async (db) => {
    let now = Date.parse('2026-09-20T05:00:00Z'); const waits: number[] = [];
    const budget = new LinkedInBudget(db, { linkedin_daily_page_limit: 10, linkedin_block_cooldown_hours: 72, linkedin_min_seconds_between_pages: 10 }, () => new Date(now), 'local', async (ms) => { waits.push(ms); now += ms; });
    assert.equal(await budget.reserve('job-search'), true); assert.deepEqual(waits, []);
    now += 3_000;                                                     // the first page took 3 seconds to load
    assert.equal(await budget.reserve('job-detail'), true); assert.deepEqual(waits, [7_000]);   // only the remaining 7 seconds are waited
    now += 12_000;                                                    // slower than the gap already: no extra wait
    assert.equal(await budget.reserve('job-search'), true); assert.deepEqual(waits, [7_000]);
    assert.equal(budget.used(), 3);
    budget.recordBlock('HTTP 429');
    assert.equal(await budget.reserve('job-search'), false); assert.deepEqual(waits, [7_000]); assert.equal(budget.used(), 3);
  });
});
