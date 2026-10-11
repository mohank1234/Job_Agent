import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/database.js';
import { parseHiringTeamEntry } from '../src/sources/browser.js';
import { prepareOutreachForJobs } from '../src/outreach-framework.js';

const config = await loadConfig(process.cwd());

test('the "Meet the hiring team" entry on a LinkedIn job page becomes a name, headline and clean profile link', () => {
  // Text copied from a real job page (2026-10-11), person details replaced.
  const member = parseHiringTeamEntry('Asha Rao • 2nd Technical HR Recruiter @ Acme Pvt Ltd | MBA in Human Resource Job poster', 'https://www.linkedin.com/in/asha-rao-123/?miniProfileUrn=abc');
  assert.deepEqual(member, { name: 'Asha Rao', title: 'Technical HR Recruiter @ Acme Pvt Ltd | MBA in Human Resource', profileUrl: 'https://www.linkedin.com/in/asha-rao-123' });
  assert.equal(parseHiringTeamEntry('Someone • 3rd+ Manager', 'https://example.com/in/someone'), undefined, 'only linkedin.com profile links count');
  assert.equal(parseHiringTeamEntry('', 'https://www.linkedin.com/in/x'), undefined);
});

test('the person who posted the job is written to first, ahead of the other people found at the company', async () => {
  const root = await mkdtemp(join(tmpdir(), 'job-link-poster-'));
  const db = openDatabase(root);
  try {
    await mkdir(join(root, 'config'));
    const others = ['Rita Recruiter', 'Hari HR', 'Mona QA'].map((name, index) => ({ company: 'Acme', name, title: index === 2 ? 'QA Manager' : 'Technical Recruiter', profileUrl: `https://linkedin.com/in/${name.toLowerCase().replace(/\W+/g, '-')}`, relevanceScore: 90 }));
    await writeFile(join(root, 'config', 'contacts.json'), JSON.stringify({ contacts: others, sources: [] }));
    const team = [{ name: 'Pia Poster', title: 'Talent Acquisition', profileUrl: 'https://www.linkedin.com/in/pia-poster' }];
    const now = new Date().toISOString();
    const id = Number(db.prepare("INSERT INTO jobs(company,title,location,job_url,source,detected_at,description,fit_score,status,evidence_mode,metadata,updated_at) VALUES('Acme','SDET','Hyderabad','https://example.com/j/1','linkedin',?,'Playwright',90,'REVIEW','live',?,?)").run(now, JSON.stringify({ hiringTeam: team }), now).lastInsertRowid);
    const cfg = structuredClone(config); cfg.outreach.max_contacts_per_job = 3; cfg.outreach.max_contacts_per_company = 3; cfg.outreach.same_company_cooldown_days = 0;
    const result = await prepareOutreachForJobs(root, db, cfg, 'r', [id]);
    assert.equal(result.prepared, 3, 'still at most three people for the company');
    const names = (db.prepare('SELECT c.name FROM outreach o JOIN contacts c ON c.id=o.contact_id ORDER BY o.id').all() as Array<{ name: string }>).map((row) => row.name);
    assert.equal(names[0], 'Pia Poster'); assert.equal(names.length, 3);
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});
