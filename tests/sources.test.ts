import assert from 'node:assert/strict';
import { test } from 'node:test';
import { sourceSettingsSchema, sourceSchema } from '../src/sources/settings.js';
import { canonicalUrl, normalizeJob, parsePostedAt, plainText } from '../src/sources/normalize.js';
import { PublicApiJobSource } from '../src/sources/public-api.js';
import { discoverConfiguredSources } from '../src/sources/live.js';
import { SourceAccessError, fetchJson } from '../src/sources/http.js';
import { jobPostingFromJsonLd } from '../src/sources/browser.js';
import { linkedinSearchUrls } from '../src/sources/linkedin.js';
import { loadConfig } from '../src/config.js';
import type { Job } from '../src/core/types.js';
import type { SourceResult } from '../src/sources/live.js';

const config = await loadConfig(process.cwd());
const job: Job = { company: 'Example', title: 'QA Engineer', location: 'Hyderabad', workMode: 'Hybrid', jobUrl: 'https://jobs.example.org/1?utm_source=search', source: 'controlled-test', postedAt: '', description: '<p>Manual QA and API testing</p>' };

test('source settings validate companies, duplicate names, URLs and limits', () => {
  assert.throws(() => sourceSettingsSchema.parse({ sources: [{ name: 'x', type: 'greenhouse', company: 'X', board: '../bad' }] }));
  assert.throws(() => sourceSettingsSchema.parse({ sources: [{ name: 'x', type: 'ashby', company: 'X', board: 'x', max_jobs: 0 }] }));
  assert.throws(() => sourceSettingsSchema.parse({ sources: ['x', 'x'].map(name => ({ name, type: 'ashby', company: 'X', board: 'x' })) }));
  assert.throws(() => sourceSettingsSchema.parse({ sources: [{ name: 'x', type: 'browser', urls: ['file:///tmp/jobs'], cardSelector: '.card', titleSelector: 'h2', linkSelector: 'a' }] }));
  assert.equal(sourceSettingsSchema.parse({ sources: [{ name: 'x', type: 'ashby', company: 'X', board: 'x' }] }).sources[0].enabled, true);
});

test('normalization preserves unknown posting dates and strips HTML/tracking', () => {
  const normalized = normalizeJob(job);
  assert.equal(normalized.postedAt, '');
  assert.equal(normalized.description, 'Manual QA and API testing');
  assert.equal(normalized.jobUrl, 'https://jobs.example.org/1');
  assert.ok(normalized.discoveredAt);
  assert.equal(plainText('&lt;p&gt;API &amp; QA&lt;/p&gt;'), 'API & QA');
  assert.equal(canonicalUrl('https://careers.example.org/jobs/?gh_jid=42&utm_medium=ads#apply'), 'https://careers.example.org/jobs?gh_jid=42');
  assert.throws(() => normalizeJob({ ...job, jobUrl: 'javascript:alert(1)' }));
});

test('publication date parsing is conservative and never invents freshness', () => {
  const now = new Date('2026-09-16T10:00:00Z');
  assert.equal(parsePostedAt(undefined, now), '');
  assert.equal(parsePostedAt('unknown', now), '');
  assert.equal(parsePostedAt('Posted 8 hours ago', now), '2026-09-16T01:00:00.000Z');
  assert.equal(parsePostedAt('Posted 30+ Days Ago', now), '2026-08-16T10:00:00.000Z');
  assert.equal(parsePostedAt('2026-09-15T23:00:00+05:30', now), '2026-09-15T17:30:00.000Z');
});

test('JSON-LD reads nested JobPosting graphs', () => {
  assert.equal(jobPostingFromJsonLd([{ '@graph': [{ '@type': 'Organization' }, { '@type': 'JobPosting', datePosted: '2026-09-15' }] }])?.datePosted, '2026-09-15');
  assert.equal(jobPostingFromJsonLd([{ '@type': 'Organization' }]), undefined);
});

test('Greenhouse uses first_published, never updated_at, and preserves ATS identity', async t => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({ jobs: [
    { id: 10, title: 'QA Engineer', company_name: 'Verified Company', location: { name: 'Hyderabad' }, absolute_url: 'https://careers.example.com/qa?gh_jid=10', first_published: '2026-09-01T10:00:00Z', updated_at: new Date().toISOString(), content: '<p>Playwright testing</p>' },
    { id: 11, title: 'QA Engineer II', location: { name: 'Hyderabad' }, absolute_url: 'https://careers.example.com/qa?gh_jid=11', updated_at: new Date().toISOString(), content: '<p>API testing</p>' }
  ] }));
  const source = new PublicApiJobSource(sourceSchema.parse({ name: 'Example GH', type: 'greenhouse', board: 'example', company: 'Example' }) as ConstructorParameters<typeof PublicApiJobSource>[0]);
  const jobs = await source.discover(config);
  assert.equal(jobs.length, 2);
  assert.equal(jobs[0].company, 'Verified Company');
  assert.equal(jobs[0].postedAt, '2026-09-01T10:00:00.000Z');
  assert.equal(jobs[1].postedAt, '');
  assert.equal(jobs[0].atsJobId, 'greenhouse:example:10');
  assert.equal(jobs[0].applicationUrl, 'https://job-boards.greenhouse.io/example/jobs/10');
  assert.equal(source.warnings.length, 1);
});

test('Lever paginates beyond the first 100 results', async t => {
  const offsets: number[] = [];
  t.mock.method(globalThis, 'fetch', async (url: string) => {
    const offset = Number(new URL(url).searchParams.get('skip')); offsets.push(offset);
    const size = offset === 0 ? 100 : 3;
    return Response.json(Array.from({ length: size }, (_, index) => ({ id: String(offset + index), text: 'QA Engineer', categories: { location: 'Hyderabad' }, createdAt: 1789516800000, hostedUrl: `https://jobs.lever.co/example/${offset + index}`, applyUrl: `https://jobs.lever.co/example/${offset + index}/apply`, descriptionPlain: 'API testing', lists: [{ text: 'Requirements', content: '<li>Java</li>' }] })));
  });
  const source = new PublicApiJobSource(sourceSchema.parse({ name: 'Lever', type: 'lever', board: 'example', company: 'Example' }) as ConstructorParameters<typeof PublicApiJobSource>[0]);
  const jobs = await source.discover(config);
  assert.equal(jobs.length, 103); assert.deepEqual(offsets, [0, 100]);
  assert.match(jobs[0].description, /Requirements\nJava/);
  assert.equal(source.truncated, false);
});

test('Ashby honors unlisted flags and secondary locations; limits are explicit', async t => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({ jobs: [
    { id: 'private', isListed: false },
    ...['a', 'b'].map(id => ({ id, title: 'QA Engineer', isListed: true, location: 'Europe', secondaryLocations: [{ location: 'India' }], isRemote: true, jobUrl: `https://jobs.ashbyhq.com/example/${id}`, applyUrl: `https://jobs.ashbyhq.com/example/${id}/application`, publishedAt: '2026-09-16T10:00:00Z', descriptionPlain: 'QA testing' }))
  ] }));
  const source = new PublicApiJobSource(sourceSchema.parse({ name: 'Ashby', type: 'ashby', board: 'example', company: 'Example', max_jobs: 1 }) as ConstructorParameters<typeof PublicApiJobSource>[0]);
  const jobs = await source.discover(config);
  assert.equal(jobs.length, 1); assert.equal(jobs[0].externalId, 'a'); assert.equal(jobs[0].location, 'Europe; India'); assert.equal(jobs[0].workMode, 'Remote'); assert.equal(source.truncated, true);
});

test('source failure does not discard jobs from independent sources', async t => {
  t.mock.method(globalThis, 'fetch', async (url: string) => url.includes('/bad') ? new Response('No board', { status: 404 }) : Response.json({ jobs: [{ id: 'x', title: 'QA Engineer', jobUrl: 'https://jobs.ashbyhq.com/good/x', publishedAt: '2026-09-16T10:00:00Z', descriptionPlain: 'QA testing' }] }));
  const settings = sourceSettingsSchema.parse({ sources: ['bad', 'good'].map(board => ({ name: board, type: 'ashby', board, company: board })) });
  const results = await discoverConfiguredSources(process.cwd(), config, settings, async () => { throw new Error('Public source must not launch browser'); });
  assert.equal(results.jobs.length, 1); assert.equal(results.sources.find(source => source.name === 'bad')?.status, 'FAILED'); assert.equal(results.sources.find(source => source.name === 'good')?.status, 'SUCCESS');
});

test('HTTP restrictions stop immediately without retry', async t => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => { calls++; return new Response('', { status: 429 }); });
  await assert.rejects(fetchJson('https://jobs.example.org'), (error: unknown) => error instanceof SourceAccessError && error.status === 'BLOCKED_SECURITY');
  assert.equal(calls, 1);
});

test('Workday hydrates descriptions and stops further requests on a restricted detail', async t => {
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async (url: string) => {
    calls++;
    if (url.endsWith('/jobs')) return Response.json({ total: 3, jobPostings: ['a', 'b', 'c'].map(id => ({ title: 'QA Engineer', externalPath: `/job/India/${id}`, postedOn: 'Posted Today' })) });
    if (url.endsWith('/b')) return new Response('', { status: 403 });
    return Response.json({ jobPostingInfo: { title: 'QA Engineer', jobReqId: 'a', location: 'Hyderabad', jobDescription: '<p>Manual QA</p>', startDate: '2026-09-15', externalUrl: 'https://example.myworkdayjobs.com/Careers/job/India/a' } });
  });
  const source = new PublicApiJobSource(sourceSchema.parse({ name: 'Workday', type: 'workday', company: 'Example', url: 'https://example.myworkdayjobs.com/Careers', tenant: 'example', site: 'Careers' }) as ConstructorParameters<typeof PublicApiJobSource>[0]);
  await assert.rejects(source.discover(config), (error: unknown) => error instanceof SourceAccessError && error.status === 'BLOCKED_SECURITY');
  assert.equal(calls, 3); assert.equal(source.collected.length, 1); assert.equal(source.collected[0].description, 'Manual QA');
});

test('LinkedIn search derives roles, locations, remote and age from actual config, priority locations first', () => {
  const urls = linkedinSearchUrls(config, 200).map(url => new URL(url));
  assert.equal(urls[0].searchParams.get('keywords'), config.search.role_keywords[0]); assert.equal(urls[0].searchParams.get('location'), 'Hyderabad');
  assert.equal(urls[0].searchParams.get('f_TPR'), `r${config.search.max_age_hours * 3600}`);
  const firstRemote = urls.find(url => url.searchParams.get('f_WT') === '2')!;
  assert.equal(firstRemote.searchParams.get('location'), 'India');
  assert.ok(urls.indexOf(firstRemote) >= config.search.role_keywords.length, 'every Hyderabad search comes before the first Remote one');
  assert.ok(urls.some(url => url.searchParams.get('location') === 'Bengaluru'));
});

test('empty live config is an actionable error, never a successful fixture run', async () => {
  await assert.rejects(discoverConfiguredSources(process.cwd(), config, sourceSettingsSchema.parse({}), async () => { throw new Error('unexpected'); }), /No enabled live sources/);
});

test('completed sources stream once, including jobs collected before a source restriction', async t => {
  let callbackCompleted = false;
  t.mock.method(globalThis, 'fetch', async (url: string) => {
    if (url.includes('/workday/') || url.includes('/wday/cxs/')) {
      if (url.endsWith('/jobs')) return Response.json({ total: 2, jobPostings: ['a', 'b'].map(id => ({ title: 'QA Engineer', externalPath: `/job/India/${id}` })) });
      if (url.endsWith('/b')) return new Response('', { status: 403 });
      return Response.json({ jobPostingInfo: { title: 'QA Engineer', jobReqId: 'a', location: 'Hyderabad', jobDescription: 'Manual QA', startDate: '2026-09-15', externalUrl: 'https://example.myworkdayjobs.com/Careers/job/India/a' } });
    }
    assert.equal(callbackCompleted, true, 'next source must wait for previous source persistence in this serial run');
    return Response.json({ jobs: [] });
  });
  const settings = sourceSettingsSchema.parse({ concurrency: 1, sources: [
    { name: 'workday', type: 'workday', company: 'Example', url: 'https://example.myworkdayjobs.com/Careers', tenant: 'example', site: 'Careers' },
    { name: 'ashby', type: 'ashby', company: 'Example', board: 'example' }
  ] });
  const callbacks: Array<{ jobs: number; result: SourceResult }> = [];
  const result = await discoverConfiguredSources(process.cwd(), config, settings, async () => { throw new Error('No browser needed'); }, async (jobs, source) => {
    await Promise.resolve(); callbacks.push({ jobs: jobs.length, result: source }); callbackCompleted = true;
  });
  assert.equal(callbacks.length, 2); assert.equal(result.jobs.length, 1);
  assert.equal(callbacks[0].result.status, 'BLOCKED_SECURITY'); assert.equal(callbacks[0].jobs, 1);
  assert.equal(callbacks[1].result.status, 'SUCCESS');
});

test('source persistence failure is fatal and never invokes callback twice or starts next source', async t => {
  let requests = 0; let callbacks = 0;
  t.mock.method(globalThis, 'fetch', async () => { requests++; return Response.json({ jobs: [] }); });
  const settings = sourceSettingsSchema.parse({ concurrency: 1, sources: ['one', 'two'].map(board => ({ name: board, type: 'ashby', board, company: 'Example' })) });
  await assert.rejects(discoverConfiguredSources(process.cwd(), config, settings, async () => { throw new Error('No browser'); }, async () => { callbacks++; throw new Error('SQLite disk full'); }), /Failed to persist completed discovery sources/);
  assert.equal(requests, 1); assert.equal(callbacks, 1);
});
