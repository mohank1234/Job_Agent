import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/database.js';
import type { Contact } from '../src/core/types.js';
import { audienceOf, buildConnectionNote } from '../src/outreach.js';
import { outreachSignature, focusSkills, pickBalanced, prepareOutreachForJobs, rankContacts } from '../src/outreach-framework.js';

const config = await loadConfig(process.cwd());
const person = (name: string, title: string, score = 0): Contact => ({ company: 'Acme', name, title, profileUrl: `https://linkedin.com/in/${name.toLowerCase().replace(/\W+/g, '-')}`, relevanceScore: score });
const base = { firstName: 'Asha', company: 'Acme', title: 'SDET', candidateName: 'Test Candidate', specialization: 'Selenium, Playwright and Java automation', years: 5 };

test('who a contact is decides the voice of the note: HR/recruiting, managers, or company leadership', () => {
  assert.deepEqual(['Talent Acquisition Manager at Infor', 'Recruiter @ Infor', 'HR Manager', 'Head of Talent Acquisition', 'Chief People Officer'].map(audienceOf), ['HR', 'HR', 'HR', 'HR', 'HR']);
  assert.deepEqual(['QA Manager', 'Engineering Manager at Acme', 'Director of Engineering', 'Hiring Manager - Quality'].map(audienceOf), ['MANAGER', 'MANAGER', 'MANAGER', 'MANAGER']);
  assert.deepEqual(['CEO at KTree Computer Solutions', 'Founder & CEO', 'CTO', 'Co-founder', 'Managing Director'].map(audienceOf), ['EXEC', 'EXEC', 'EXEC', 'EXEC', 'EXEC']);
});

test('notes read differently for HR, managers and leadership, always name the role and company, and stay within 300 characters', () => {
  const hr = buildConnectionNote({ ...base, audience: 'HR' }, 300);
  const manager = buildConnectionNote({ ...base, audience: 'MANAGER' }, 300);
  const exec = buildConnectionNote({ ...base, audience: 'EXEC' }, 300);
  assert.match(hr, /guidance on the hiring process/); assert.match(manager, /short chat about what your team/); assert.match(exec, /quality of what Acme builds/);
  assert.equal(new Set([hr, manager, exec]).size, 3);
  for (const note of [hr, manager, exec]) { assert.ok([...note].length <= 300, note); assert.match(note, /SDET role at Acme/); assert.match(note, /5 years of experience in Selenium, Playwright and Java automation/); assert.ok(note.endsWith('Test Candidate')); }
  assert.ok(!/applied/.test(hr), 'no claim of having applied unless an application was submitted');
  assert.match(buildConnectionNote({ ...base, audience: 'HR', submitted: true }, 300), /I've applied for the SDET role at Acme/);
  assert.ok(!/years of experience/.test(buildConnectionNote({ ...base, years: null, audience: 'MANAGER' }, 300)), 'the years appear only when the profile states them');
  assert.ok([...buildConnectionNote({ ...base, title: 'A very long job title '.repeat(20), audience: 'EXEC' }, 300)].length <= 300, 'a note that cannot fit shrinks to a shorter honest one');
});

test('the skills led with follow the kind of job and come only from the candidate profile', () => {
  const candidate = config.candidate;
  const automation = focusSkills('SDET - Test Automation Engineer', candidate);
  assert.match(automation, / automation$/); for (const skill of automation.replace(/ automation$/, '').split(/, | and /)) assert.ok(candidate.automation_skills.includes(skill), skill);
  if (candidate.ai_testing_skills.length) { const ai = focusSkills('GenAI Test Engineer (LLM evaluation)', candidate); for (const skill of ai.split(/, | and /)) assert.ok(candidate.ai_testing_skills.includes(skill), skill); }
  const manual = focusSkills('Manual QA Tester', candidate);
  for (const skill of manual.split(/, | and /).filter(Boolean)) assert.ok([...candidate.skills, ...candidate.automation_skills].includes(skill) || manual === focusSkills('QA Engineer', candidate), skill);
});

test('for each company: one recruiter/HR and one manager first, then the next best, and a CEO-level contact only if a place is left', () => {
  const everyone = [person('Rita Recruiter', 'Technical Recruiter'), person('Hari HR', 'HR Manager'), person('Mona QA', 'QA Manager'), person('Eddie Eng', 'Engineering Manager'), person('Carl CEO', 'CEO'), person('Tina CTO', 'CTO')];
  const three = pickBalanced(everyone, 3);
  assert.deepEqual(three.slice(0, 2).map((contact) => contact.name), ['Rita Recruiter', 'Mona QA'], 'the best recruiter and the best manager come first');
  assert.ok(['Hari HR', 'Eddie Eng'].includes(three[2].name), 'the third place goes to the next best HR or manager, not to leadership');
  assert.deepEqual(pickBalanced([person('Rita Recruiter', 'Recruiter'), person('Carl CEO', 'CEO'), person('Tina CTO', 'CTO')], 3).map((contact) => contact.name), ['Rita Recruiter', 'Carl CEO'], 'at most one leader, and only after the HR and managers');
  assert.deepEqual(pickBalanced(everyone, 2).map((contact) => contact.name), ['Rita Recruiter', 'Mona QA']);
  assert.equal(pickBalanced(everyone, 0).length, 0);
  assert.ok(rankContacts([person('Carl CEO', 'CEO'), person('Rita Recruiter', 'Recruiter')])[0].name === 'Rita Recruiter');
});

test('no more than the per-company limit is ever drafted, however many jobs or runs, and the drafts are tailored to each person', async () => {
  const root = await mkdtemp(join(tmpdir(), 'job-link-company-cap-'));
  const db = openDatabase(root);
  try {
    await mkdir(join(root, 'config'));
    const pool = [person('Rita Recruiter', 'Technical Recruiter', 90), person('Hari HR', 'HR Manager', 85), person('Mona QA', 'QA Manager', 90), person('Eddie Eng', 'Engineering Manager', 85), person('Carl CEO', 'CEO', 65)];
    await writeFile(join(root, 'config', 'contacts.json'), JSON.stringify({ contacts: pool, sources: [] }));
    const job = (title: string) => Number(db.prepare("INSERT INTO jobs(company,title,location,job_url,source,detected_at,description,fit_score,status,evidence_mode,updated_at) VALUES('Acme',?,'Remote',?,'linkedin',?,'QA',90,'REVIEW','live',?)").run(title, `https://example.com/j/${encodeURIComponent(title)}`, new Date().toISOString(), new Date().toISOString()).lastInsertRowid);
    const first = job('SDET'); const second = job('QA Engineer');
    const cfg = structuredClone(config); cfg.outreach.max_contacts_per_job = 3; cfg.outreach.max_contacts_per_company = 3; cfg.outreach.same_company_cooldown_days = 0;
    const result = await prepareOutreachForJobs(root, db, cfg, 'r', [first, second]);
    assert.equal(result.prepared, 3, 'three people for Acme, not three per job');
    const rows = db.prepare('SELECT c.name, o.message FROM outreach o JOIN contacts c ON c.id=o.contact_id ORDER BY o.id').all() as Array<{ name: string; message: string }>;
    assert.equal(rows.length, 3); assert.ok(!rows.some((row) => row.name === 'Carl CEO'), 'the CEO comes after HR and managers');
    assert.match(rows.find((row) => row.name === 'Rita Recruiter')!.message, /guidance on the hiring process/);
    assert.match(rows.find((row) => row.name === 'Mona QA')!.message, /team/);
    for (const row of rows) { assert.equal([...row.message].length, 300, row.message); assert.ok(row.message.endsWith(`\n\n${outreachSignature(cfg)}`)); }
    assert.equal((await prepareOutreachForJobs(root, db, cfg, 'r2', [first, second])).prepared, 0, 'a later run adds nobody once the company has its three');
    db.prepare("UPDATE outreach SET status='DECLINED' WHERE id=(SELECT MIN(id) FROM outreach)").run();
    assert.equal((await prepareOutreachForJobs(root, db, cfg, 'r3', [first])).prepared, 1, 'someone told "never" frees a place');
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});
