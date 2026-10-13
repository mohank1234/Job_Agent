import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/database.js';
import { persistJob } from '../src/jobs.js';

const config = await loadConfig(process.cwd());
const base = { company: 'Acme', title: 'Platform Engineer', location: 'Pune, Maharashtra, India', workMode: '', jobUrl: 'https://www.linkedin.com/jobs/view/4400000001/', source: 'linkedin', postedAt: new Date(Date.now() - 2 * 3_600_000).toISOString(), description: 'We build things with Kubernetes and Go. 5+ years of experience.' };

test('a job LinkedIn emailed to the person is eligible without the role/location/skill filters, but their own rules still apply', async () => {
  const root = await mkdtemp(join(tmpdir(), 'job-link-alerted-'));
  const db = openDatabase(root);
  try {
    const alert = { sourceName: 'LinkedIn alert email' };
    const searched = persistJob(db, { ...base, externalId: '4400000001' }, config, 'live');
    assert.notEqual(searched.disposition, 'ELIGIBLE', 'the same job found by search is filtered on role and skills');
    const alerted = persistJob(db, { ...base, jobUrl: 'https://www.linkedin.com/jobs/view/4400000002/', externalId: '4400000002', metadata: alert }, config, 'live');
    assert.equal(alerted.disposition, 'ELIGIBLE');
    const intern = persistJob(db, { ...base, title: 'QA Intern', jobUrl: 'https://www.linkedin.com/jobs/view/4400000003/', externalId: '4400000003', metadata: alert }, config, 'live');
    assert.notEqual(intern.disposition, 'ELIGIBLE', 'internships are never applied to');
    const junior = persistJob(db, { ...base, title: 'QA Engineer', description: '1-2 years of experience in testing.', jobUrl: 'https://www.linkedin.com/jobs/view/4400000004/', externalId: '4400000004', metadata: alert }, config, 'live');
    assert.notEqual(junior.disposition, 'ELIGIBLE', 'postings under the minimum experience are skipped');
    const stale = persistJob(db, { ...base, postedAt: new Date(Date.now() - 5 * 86_400_000).toISOString(), jobUrl: 'https://www.linkedin.com/jobs/view/4400000005/', externalId: '4400000005', metadata: alert }, config, 'live');
    assert.notEqual(stale.disposition, 'ELIGIBLE', 'stale postings are still stale');
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});
