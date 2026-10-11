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
