import { rememberVerifiedAnswer, loadAnswerMemory } from '../src/answer-memory.js';
import { classifyField } from '../src/ats/answers.js';
import { canonicalUrl } from '../src/sources/normalize.js';
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { BrowserContext } from 'playwright';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/database.js';
import { persistJob, type JobInput } from '../src/jobs.js';
import { closeSession } from '../src/browser/session.js';

const config = await loadConfig(process.cwd());
const description = 'QA Engineer with 5+ years experience. Required: manual testing, functional testing, Selenium, Java and API testing. Build regression testing and automation testing for our Hyderabad team.';
const now = Date.parse('2026-09-20T12:00:00Z');
const hoursAgo = (hours: number) => new Date(now - hours * 3_600_000).toISOString();
const full = (): JobInput => ({ company: 'Lumber', title: 'Quality Assurance Automation Engineer', location: 'Hyderabad, India', workMode: 'On-site', source: 'linkedin', externalId: '4467328607', postedAt: hoursAgo(5), description,
  jobUrl: 'https://www.linkedin.com/jobs/view/4467328607?refId=first', applicationUrl: 'https://lumberfi.keka.com/careers/jobdetails/162065', metadata: { applyKind: 'external' } });
// What a later search returns for the same posting: no timestamp, no description, only the LinkedIn page as the apply target.
const thin = (ref: string): JobInput => ({ ...full(), postedAt: '', description: '', jobUrl: `https://www.linkedin.com/jobs/view/4467328607?refId=${ref}`, applicationUrl: undefined, metadata: {} });

async function withDb<T>(run: (db: ReturnType<typeof openDatabase>) => T): Promise<T> {
  const root = await mkdtemp(join(tmpdir(), 'job-link-dupes-')); const db = openDatabase(root);
  try { return run(db); } finally { db.close(); await rm(root, { recursive: true, force: true }); }
}

test('a thinner duplicate found by another search never overwrites a fully evaluated posting', async () => {
  await withDb((db) => {
    const first = persistJob(db, full(), config, 'live', now);
    assert.equal(first.disposition, 'ELIGIBLE'); assert.ok(first.score! >= config.application.review_min_score);
    for (const ref of ['second', 'third', 'fourth']) {
      const again = persistJob(db, thin(ref), config, 'live', now);
      assert.equal(again.id, first.id); assert.equal(again.disposition, 'ELIGIBLE', again.reason); assert.equal(again.score, first.score);
    }
    const row = db.prepare('SELECT status,fit_score,posted_at,application_url,length(description) dlen,metadata FROM jobs WHERE id=?').get(first.id) as { status: string; fit_score: number; posted_at: string; application_url: string; dlen: number; metadata: string };
    assert.equal(row.status, 'REVIEW'); assert.equal(row.fit_score, first.score); assert.equal(row.posted_at, hoursAgo(5));
    assert.equal(row.application_url, 'https://lumberfi.keka.com/careers/jobdetails/162065'); assert.ok(row.dlen >= description.length);
    assert.equal((JSON.parse(row.metadata) as { applyKind?: string }).applyKind, 'external');
  });
});

test('a fuller later copy still improves an earlier thin one', async () => {
  await withDb((db) => {
    const early = persistJob(db, thin('early'), config, 'live', now);
    assert.equal(early.disposition, 'UNKNOWN');
    const later = persistJob(db, full(), config, 'live', now);
    assert.equal(later.id, early.id); assert.equal(later.disposition, 'ELIGIBLE');
    assert.equal((db.prepare('SELECT status FROM jobs WHERE id=?').get(later.id) as { status: string }).status, 'REVIEW');
  });
});

test('closing a crashed browser gives up after the timeout instead of holding the process (and run lock) forever', async () => {
  const hung = { close: () => new Promise<void>(() => undefined) } as unknown as BrowserContext;
  const started = Date.now();
  await closeSession(hung, 200);
  assert.ok(Date.now() - started < 2000);
  await closeSession({ close: async () => undefined } as unknown as BrowserContext, 200);
  await closeSession({ close: async () => { throw new Error('already closed'); } } as unknown as BrowserContext, 200);
});


test('closed postings remain closed on rediscovery and new IDs remain distinct', async()=>{
  await withDb(db=>{
    const job={...full(),description:'Not currently accepting applications'};
    const first=persistJob(db,job,config,'live',now);
    assert.equal(first.disposition,'CLOSED_JOB');assert.equal(first.score,null);
    assert.equal(persistJob(db,full(),config,'live',now).disposition,'CLOSED_JOB');
    const fresh=persistJob(db,{...full(),externalId:'9999999999',jobUrl:'https://www.linkedin.com/jobs/view/9999999999',applicationUrl:undefined},config,'live',now);
    assert.notEqual(fresh.id,first.id);assert.equal(fresh.disposition,'ELIGIBLE');
  });
});

test('verified answer memory survives reload and reuses equivalent facts without legal or skill guesses', async()=>{
  await withDb(db=>{
    rememberVerifiedAnswer(db,'Total experience?','7');
    rememberVerifiedAnswer(db,'How many years of Playwright experience?','3');
    rememberVerifiedAnswer(db,'What is your current CTC?','12 LPA');
    rememberVerifiedAnswer(db,'Do you agree to Acme terms?','Yes');
    const answers={};loadAnswerMemory(db,answers);
    const answer=(label:string)=>classifyField({label,required:true,type:'text'},answers).answer;
    assert.equal(answer('How many years of professional experience?'),'7');
    assert.equal(answer('Overall years of experience?'),'7');
    assert.equal(answer('Playwright experience in years?'),'3');
    assert.equal(answer('Years working with Playwright?'),'3');
    assert.equal(answer('Current compensation?'),'12 LPA');
    assert.equal(answer('How many years of Python experience?'),undefined);
    assert.equal(answer('Do you agree to Acme terms?'),undefined);
    rememberVerifiedAnswer(db,'Total years of work experience','8');
    loadAnswerMemory(db,answers);assert.equal(answer('Total experience'),'8');
  });
});

test('LinkedIn slug, tracking and search URLs share a canonical posting identity',()=>{
  const expected='https://www.linkedin.com/jobs/view/1234567890';
  for(const url of ['https://in.linkedin.com/jobs/view/qa-engineer-1234567890?refId=abc','https://www.linkedin.com/jobs/view/1234567890?trackingId=x','https://www.linkedin.com/jobs/search/?currentJobId=1234567890'])assert.equal(canonicalUrl(url),expected);
});
