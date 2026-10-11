import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseAlertFile } from '../src/alert-import.js';

test('alert-email jobs: only LinkedIn job links, one per job number, newest first, tracking removed', () => {
  const jobs = parseAlertFile(JSON.stringify([
    { url: 'https://www.linkedin.com/comm/jobs/view/4471000001/?trackingId=abc&refId=x', title: 'QA Engineer', company: 'Acme', location: 'Hyderabad', receivedAt: '2026-10-11T03:00:00Z' },
    { url: 'https://www.linkedin.com/jobs/view/senior-sdet-at-beta-4471000002?trk=email', title: 'Senior SDET', company: 'Beta', receivedAt: '2026-10-11T05:00:00Z' },
    { url: 'https://www.linkedin.com/jobs/view/4471000001/', title: 'QA Engineer (repeat)', company: 'Acme' },
    { url: 'https://www.naukri.com/job-listings-123', title: 'Not LinkedIn', company: 'X' },
    { url: 'https://www.linkedin.com/in/someone', title: 'A profile, not a job', company: 'Y' }
  ]));
  assert.deepEqual(jobs.map((job) => job.url), ['https://www.linkedin.com/jobs/view/4471000002/', 'https://www.linkedin.com/jobs/view/4471000001/']);
  assert.equal(jobs[1].company, 'Acme'); assert.equal(jobs[1].location, 'Hyderabad');
  assert.deepEqual(parseAlertFile('[]'), []);
  assert.equal(parseAlertFile(JSON.stringify({ jobs: [{ url: 'https://www.linkedin.com/jobs/view/4471000009/', title: 'T', company: 'C' }] })).length, 1);
});

test('title, company and location come from the LinkedIn page, not the alert email', async () => {
  const { parseTopCard } = await import('../src/alert-import.js');
  assert.deepEqual(parseTopCard('QA Analyst Lead | ENGIE India | LinkedIn', ['ENGIE India', 'QA Analyst Lead', 'Pune City, Maharashtra, India · 3 days ago · Over 100 people clicked apply', 'Hybrid']),
    { title: 'QA Analyst Lead', company: 'ENGIE India', location: 'Pune City, Maharashtra, India' });
  assert.deepEqual(parseTopCard('Senior QA Engineer - Remote | Kake | LinkedIn', []), { title: 'Senior QA Engineer - Remote', company: 'Kake' });
  assert.deepEqual(parseTopCard('Sign in | LinkedIn', []), {});
});

test('a tab title that does not resemble the alerted job is not trusted', async () => {
  const { similarTitles } = await import('../src/alert-import.js');
  assert.equal(similarTitles('Senior QA Engineer - Remote', 'Senior QA Engineer'), true);
  assert.equal(similarTitles('QA Analyst Lead', 'QA Analyst Lead'), true);
  assert.equal(similarTitles('Get Ahead of the Job Search', 'Quality Assurance Automation Engineer'), false);
});

test('alerts from emails older than the freshness window are not opened', async () => {
  const { freshAlerts } = await import('../src/alert-import.js');
  const now = Date.parse('2026-10-11T12:00:00Z');
  const mk = (receivedAt?: string) => ({ url: 'https://www.linkedin.com/jobs/view/1/', title: 't', company: 'c', receivedAt });
  assert.equal(freshAlerts([mk('2026-10-11T03:00:00Z'), mk('2026-10-09T03:00:00Z'), mk(undefined)], 24, now).length, 2, 'the 2-day-old email goes, an undated one stays');
});
