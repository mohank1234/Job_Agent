import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { tmpdir } from 'node:os';
import { chromium, type Browser } from 'playwright';
import { loadConfig } from '../src/config.js';
import { discoveryTimings, hydrateJob, relativeAgo } from '../src/sources/browser.js';
import { LinkedInJobSource } from '../src/sources/linkedin.js';
import { sourceSchema } from '../src/sources/settings.js';
import type { Job } from '../src/core/types.js';

let browser: Browser;
before(async () => { discoveryTimings.scrollGrowMs = 1500; browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

const config = await loadConfig(process.cwd());
type LinkedInSettings = Extract<ReturnType<typeof sourceSchema.parse>, { type: 'linkedin' }>;

// LinkedIn's results list is virtualized: 25 results per page, but only the first 7 are drawn until the list is scrolled.
const resultsPage = (firstId: number, total: number) => `<html><body><div id="list" style="height:260px;overflow:auto"><ul id="ul"></ul></div><script>
var shown = 0;
function add(n) { for (var i = 0; i < n && shown < ${total}; i++, shown++) { var id = ${firstId} + shown; var li = document.createElement('li'); li.className = 'base-search-card';
  li.innerHTML = '<a class="base-card__full-link" href="/jobs/view/' + id + '/"></a><h3 class="base-search-card__title">Product Engineer ' + id + '</h3><h4 class="base-search-card__subtitle">Acme</h4><span class="job-search-card__location">Hyderabad, India</span>' + (shown % 10 === 3 ? '<span>Viewed</span>' : '<time datetime="2026-09-21">10 minutes ago</time>'); document.getElementById('ul').appendChild(li); } }
add(7);
document.getElementById('list').addEventListener('scroll', function () { setTimeout(function () { add(6); }, 50); });
</script></body></html>`;

async function discover(urls: string[], pageFor: (start: number) => string) {
  const context = await browser.newContext();
  const hits: string[] = [];
  await context.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (!url.pathname.startsWith('/jobs/search')) return route.fulfill({ contentType: 'text/html', body: '<html><body>job</body></html>' });
    hits.push(url.search);
    return route.fulfill({ contentType: 'text/html', body: pageFor(Number(url.searchParams.get('start') ?? 0)) });
  });
  const settings = sourceSchema.parse({ name: 'LinkedIn', type: 'linkedin', urls, max_pages: 5 }) as LinkedInSettings;
  const source = new LinkedInJobSource(context, settings, tmpdir());
  const jobs = await source.discover(config);
  await context.close();
  return { jobs, hits };
}

test('every card on a results page is read (not just the 7 drawn first), the next page is followed, and ages come from the "10 minutes ago" text', async () => {
  const { jobs, hits } = await discover(['https://www.linkedin.com/jobs/search/?keywords=QA'], (start) => resultsPage(1000 + start, start === 0 ? 25 : start === 25 ? 10 : 0));
  assert.equal(jobs.length, 35, 'a full page of 25 and then the short last page of 10');
  assert.equal(hits.length, 2, 'the short second page ends the search');
  const dated = jobs.filter((job) => job.postedAt);
  assert.ok(dated.length >= 30, 'cards showing a time have a real age');
  for (const job of dated) { const minutes = (Date.now() - Date.parse(job.postedAt)) / 60_000; assert.ok(minutes > 5 && minutes < 30, `${minutes} minutes (a date-only value would read as hours since midnight)`); }
  assert.ok(jobs.some((job) => !job.postedAt), 'a "Viewed" card has no age on the card; the job page is asked for it instead');
});

test('a page that adds nothing new (every job already collected by an earlier search) ends that search instead of paging on', async () => {
  const { jobs, hits } = await discover(['https://www.linkedin.com/jobs/search/?keywords=A', 'https://www.linkedin.com/jobs/search/?keywords=B'], () => resultsPage(2000, 25));
  assert.equal(jobs.length, 25);
  assert.equal(hits.length, 3, 'search A: page 1 (25 new) and page 2 (all repeats); search B: page 1 (all repeats)');
});

test('a phrase like "10 minutes ago" or "Reposted 1 week ago" is found in card or page text and normalised', () => {
  assert.equal(relativeAgo('10 minutes ago\n Within the past 24 hours'), '10 minutes ago');
  assert.equal(relativeAgo('Hyderabad, Telangana, India · Reposted 1 week ago · Over 100 people clicked apply'), '1 week ago');
  assert.equal(relativeAgo('Posted 3 weeks ago'), '3 weeks ago');
  assert.equal(relativeAgo('about 2 hours ago'), '2 hours ago');
  assert.equal(relativeAgo('1 day ago'), '1 day ago');
  assert.equal(relativeAgo('Viewed'), ''); assert.equal(relativeAgo('3 years ago'), '');
});

const job = (postedAt = ''): Job => ({ company: 'Acme', title: 'QA Engineer', location: 'Hyderabad', workMode: '', jobUrl: 'https://www.linkedin.com/jobs/view/1/', source: 'linkedin', postedAt, description: '' });
const detailPage = (topLine: string) => `<html><body><div>QA Engineer</div><div>${topLine}</div><div>About the job\n${'Manual and automation testing with Selenium. '.repeat(4)}\nSet alert for similar jobs</div></body></html>`;
const detail = { descriptionTextAnchor: { start: 'About the job', end: 'Set alert for similar jobs' } };

test('a job page that states its age only in text ("Reposted 1 week ago") gives the job a real age, so an old repost is recognised as old', async () => {
  const page = await browser.newPage();
  await page.setContent(detailPage('Hyderabad, Telangana, India · Reposted 1 week ago · Over 100 people clicked apply'));
  const old = await hydrateJob(page, job(), detail);
  const days = (Date.now() - Date.parse(old.postedAt)) / 86_400_000;
  assert.ok(days > 7 && days < 16, `${days} days`);
  await page.setContent(detailPage('Hyderabad, Telangana, India · 3 hours ago · 25 applicants'));
  const fresh = await hydrateJob(page, job(), detail);
  assert.ok((Date.now() - Date.parse(fresh.postedAt)) / 3_600_000 < 6);
  await page.setContent(detailPage('Hyderabad, Telangana, India'));
  const card = new Date(Date.now() - 3_600_000).toISOString();
  assert.equal((await hydrateJob(page, job(card), detail)).postedAt, card, 'with nothing on the page, the age from the card is kept');
  await page.close();
});

async function discoverWith(urls: string[], respond: (keywords: string) => 'timeout' | string) {
  const context = await browser.newContext();
  await context.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (!url.pathname.startsWith('/jobs/search')) return route.fulfill({ contentType: 'text/html', body: '<html><body>job</body></html>' });
    const answer = respond(url.searchParams.get('keywords') ?? '');
    return answer === 'timeout' ? route.abort('timedout') : route.fulfill({ contentType: 'text/html', body: answer });
  });
  const settings = sourceSchema.parse({ name: 'LinkedIn', type: 'linkedin', urls, max_pages: 5 }) as LinkedInSettings;
  const source = new LinkedInJobSource(context, settings, tmpdir());
  try { return { source, jobs: await source.discover(config) }; } finally { await context.close(); }
}

// Live 2026-09-29: one search page timed out after 30 s and the whole LinkedIn source failed, so no LinkedIn job was found that day.
test('one search page that does not load is skipped with a warning, and the other searches still run', async () => {
  const { source, jobs } = await discoverWith(['https://www.linkedin.com/jobs/search/?keywords=Slow', 'https://www.linkedin.com/jobs/search/?keywords=QA'], (keywords) => keywords === 'Slow' ? 'timeout' : resultsPage(3000, 10));
  assert.equal(jobs.length, 10);
  assert.ok(source.warnings.some((warning) => /Skipped one search page that did not load \(.*ERR_TIMED_OUT/.test(warning)), source.warnings.join('\n'));
});

test('when three searches in a row cannot load, LinkedIn itself is unreachable and the source still fails', async () => {
  const urls = ['A', 'B', 'C', 'D'].map((keywords) => `https://www.linkedin.com/jobs/search/?keywords=${keywords}`);
  await assert.rejects(discoverWith(urls, () => 'timeout'), /ERR_TIMED_OUT/);
});
