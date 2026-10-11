import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { chromium, type Browser } from 'playwright';
import { hydrateJob } from '../src/sources/browser.js';
import { BrowserJobSource } from '../src/sources/browser.js';
import { browserSourceSchema } from '../src/sources/settings.js';
import { loadConfig } from '../src/config.js';
import type { Job } from '../src/core/types.js';

let browser: Browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

const baseJob: Job = { company: 'Acme', title: 'QA Engineer', location: 'Remote', workMode: 'Remote', jobUrl: 'https://example.test/job', source: 'controlled-test', postedAt: '', description: '' };
const longDescription = 'We are looking for a QA Engineer with Selenium, Playwright and API testing experience. '.repeat(6);

async function open(html: string) {
  const page = await browser.newPage();
  await page.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: html }));
  await page.goto('https://example.test/job');
  return page;
}

test('a reused show-more class matching a short unrelated badge does not win over the real, longer description', async () => {
  const page = await open(`<!doctype html><html><body>
    <div class="show-more-less-html__markup">Verified with verification</div>
    <div class="show-more-less-html__markup">${longDescription}</div>
  </body></html>`);
  const result = await hydrateJob(page, baseJob, { descriptionSelector: '.show-more-less-html__markup' });
  assert.ok(result.description.length > 100, result.description);
  assert.ok(result.description.includes('Selenium'));
  assert.ok(!result.description.startsWith('Verified with verification'));
  await page.close();
});

test('JSON-LD description wins when the CSS-matched element is only a short fragment', async () => {
  const html = `<!doctype html><html><body>
    <div class="show-more-less-html__markup">N/A</div>
    <script type="application/ld+json">${JSON.stringify({ '@type': 'JobPosting', description: `<p>${longDescription}</p>` })}</script>
  </body></html>`;
  const page = await open(html);
  const result = await hydrateJob(page, baseJob, { descriptionSelector: '.show-more-less-html__markup' });
  assert.ok(result.description.includes('Selenium'));
  assert.ok(result.description.length > 100);
  await page.close();
});

test('a single well-formed description is still used normally', async () => {
  const page = await open(`<!doctype html><html><body><div class="show-more-less-html__markup">${longDescription}</div></body></html>`);
  const result = await hydrateJob(page, baseJob, { descriptionSelector: '.show-more-less-html__markup' });
  assert.equal(result.description, longDescription.trim());
  await page.close();
});

test('a text-anchor fallback recovers the real description when the page renders it behind unstable, build-generated class names', async () => {
  const html = `<!doctype html><html><body>
    <div class="_30b97c4f c90385f9">Some unrelated premium-insights widget text that is not the job description at all, padded out.</div>
    <div class="f852cf51">About the job\n\n${longDescription}\n\nSet alert for similar jobs\n\nunrelated trailing content</div>
  </body></html>`;
  const page = await open(html);
  const result = await hydrateJob(page, baseJob, { descriptionSelector: '.show-more-less-html__markup, .jobs-description-content__text', descriptionTextAnchor: { start: 'About the job', end: 'Set alert for similar jobs' } });
  assert.ok(result.description.includes('Selenium'), result.description);
  assert.ok(!result.description.includes('unrelated trailing content'));
  assert.ok(!result.description.includes('premium-insights widget'));
  await page.close();
});

test('the text-anchor fallback is skipped when the anchor text never appears, rather than guessing', async () => {
  const page = await open('<!doctype html><html><body><div>Nothing relevant here.</div></body></html>');
  const result = await hydrateJob(page, baseJob, { descriptionTextAnchor: { start: 'About the job', end: 'Set alert for similar jobs' } });
  assert.equal(result.description, '');
  await page.close();
});

test('a duplicated screen-reader line on a search-result card never contaminates the scraped title', async () => {
  const cardHtml = `<!doctype html><html><body><ul>
    <li class="job-card-container">
      <a class="job-card-container__link" href="/jobs/view/123">QA Engineer\nQA Engineer with verification</a>
      <div class="artdeco-entity-lockup__subtitle">Acme Corp\nAcme Corp with verification</div>
    </li>
  </ul></body></html>`;
  const context = await browser.newContext();
  await context.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: cardHtml }));
  const config = await loadConfig(process.cwd());
  const source = new BrowserJobSource(context, browserSourceSchema.parse({ name: 'test', type: 'browser', urls: ['https://example.test/search'], cardSelector: '.job-card-container', titleSelector: '.job-card-container__link', companySelector: '.artdeco-entity-lockup__subtitle', linkSelector: '.job-card-container__link' }));
  const jobs = await source.discover(config);
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].title, 'QA Engineer');
  assert.equal(jobs[0].company, 'Acme Corp');
  await context.close();
});

test('a title concatenated with an identical accessibility span and no separator is also de-duplicated', async () => {
  const cardHtml = `<!doctype html><html><body><ul>
    <li class="job-card-container">
      <a class="job-card-container__link" href="/jobs/view/456">Quality Assurance EngineerQuality Assurance Engineer</a>
      <div class="artdeco-entity-lockup__subtitle">Acme Corp</div>
    </li>
  </ul></body></html>`;
  const context = await browser.newContext();
  await context.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: cardHtml }));
  const config = await loadConfig(process.cwd());
  const source = new BrowserJobSource(context, browserSourceSchema.parse({ name: 'test', type: 'browser', urls: ['https://example.test/search'], cardSelector: '.job-card-container', titleSelector: '.job-card-container__link', companySelector: '.artdeco-entity-lockup__subtitle', linkSelector: '.job-card-container__link' }));
  const jobs = await source.discover(config);
  assert.equal(jobs[0].title, 'Quality Assurance Engineer');
  await context.close();
});
