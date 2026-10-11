import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/database.js';
import { requiredExperienceYears } from '../src/fit.js';
import { levelExclusion, persistJob } from '../src/jobs.js';
import { retireExcludedApplications } from '../src/exclusions.js';
import { processEligibleJobs } from '../src/pipeline.js';
import { learnVerifiedAnswers, processApplicationQueue } from '../src/application-runner.js';
import { classifyField } from '../src/ats/answers.js';
import type { CandidateAnswer, FormField } from '../src/core/types.js';

const config = await loadConfig(process.cwd());
config.search.min_job_experience_years = 4; config.search.exclude_internships = true;

// Phrasings seen in real postings (2026-09-29/30 runs), with the level each is written for.
test('the experience a posting asks for is read from its overall requirement, not from tool-specific or company years', () => {
  const level = (description: string, title = 'QA Engineer') => requiredExperienceYears(title, description);
  assert.equal(level('2–3 years of experience as an SDET, QA Automation Engineer, or Software Test Engineer.'), 3);
  assert.equal(level('Experience: 3+ Years'), 3);
  assert.equal(level('Minimum 2 years of experience in software quality assurance.'), 2);
  assert.equal(level('0–1 year of professional experience; freshers welcome.'), 1);
  assert.equal(level('2 to 3.8 years’ experience in manual testing.'), 3.8);
  assert.equal(level('Must have 5+ years of QA experience. 2+ years of experience with Cypress.'), 5, 'the overall bar, not the tool line');
  assert.equal(level('• 4+ years in Data Quality, Data Engineering\n• 1+ years of work experience with Data Quality'), 4, 'LinkedIn "work experience with <skill>" lines are not the level');
  assert.equal(level('3-5 years of experience in test automation'), 5, 'a range counts by its top');
  assert.equal(level('CIBC is a leading bank, with a history of more than 150 years. 3+ years of performance testing experience.'), 3);
  assert.equal(level('Educational Qualification : 15 years full time education'), undefined);
  assert.equal(level('We test payments at scale.'), undefined, 'no stated experience is no evidence either way');
  assert.equal(level('Own the regression suite.', 'Lead Test Engineer (C# & Playwright- 5-7 Years)'), 7);
});

test('internships, entry-level titles and postings below the experience minimum are excluded; stating nothing is not', () => {
  const excluded = (title: string, description = 'Own the regression suite.') => levelExclusion({ title, description }, config);
  assert.equal(excluded('Quality Assurance Intern - Unpaid'), 'Internship or entry-level role');
  assert.equal(excluded('Associate QA Engineer Intern – AI & Automation'), 'Internship or entry-level role');
  assert.equal(excluded('Jr. Software Test Engineer'), 'Internship or entry-level role');
  assert.equal(excluded('QA Engineer', 'This is an unpaid role for students.'), 'Internship or entry-level role');
  assert.match(excluded('QA Tester – Java Automation', '2–3 years of experience as an SDET.') ?? '', /asks for 3 years of experience, below your minimum of 4/);
  assert.equal(excluded('Senior QA Engineer', '5+ years of experience in QA automation.'), undefined);
  assert.equal(excluded('Senior QA Engineer'), undefined);
  assert.equal(levelExclusion({ title: 'QA Intern', description: '' }, { ...config, search: { ...config.search, exclude_internships: false, min_job_experience_years: 0 } }), undefined, 'both rules can be switched off');
});

async function env() {
  const root = await mkdtemp(join(tmpdir(), 'job-link-audit-'));
  await mkdir(join(root, 'resumes'));
  await writeFile(join(root, 'resumes', 'Main.pdf'), '%PDF-1.4\nControlled resume\n');
  const db = openDatabase(root);
  db.prepare("INSERT INTO runs(id,started_at,status,mode) VALUES('r',?,'STARTED','live')").run(new Date().toISOString());
  db.prepare("INSERT INTO resumes(filename,category,keywords,updated_at,available) VALUES('Main.pdf','qa','[\"qa\"]',?,1)").run(new Date().toISOString());
  return { root, db };
}
type Db = ReturnType<typeof openDatabase>;
function seed(db: Db, o: { company?: string; title: string; description?: string; status?: string; hoursAgo?: number; application?: string }): number {
  const posted = new Date(Date.now() - (o.hoursAgo ?? 1) * 3600_000).toISOString();
  const id = Number(db.prepare(`INSERT INTO jobs(company,title,location,job_url,source,posted_at,detected_at,description,application_url,fit_score,status,evidence_mode,updated_at)
    VALUES(?,?,'Hyderabad',?,'linkedin',?,?,?,?,90,?,'live',?)`).run(o.company ?? 'Acme', o.title, `https://jobs.example.test/${Math.random()}`, posted, posted, o.description ?? 'QA role. 5+ years of QA experience.', `https://jobs.example.test/apply/${Math.random()}`, o.status ?? 'REVIEW', posted).lastInsertRowid);
  if (o.application) db.prepare("INSERT INTO applications(job_id,application_url,resume,started_at,status,updated_at) VALUES(?,?,'Main.pdf',?,?,?)").run(id, 'https://jobs.example.test/apply', posted, o.application, posted);
  return id;
}
const statusOf = (db: Db, id: number): string => (db.prepare('SELECT status FROM applications WHERE job_id=?').get(id) as { status: string }).status;

test('already-queued applications below the level are retired with their questions and unsent notes; a submitted one is never touched', async () => {
  const { root, db } = await env();
  try {
    const intern = seed(db, { title: 'Quality Assurance Intern - Unpaid', application: 'READY_FOR_SUBMISSION' });
    const junior = seed(db, { title: 'QA Tester', description: '2–3 years of experience as an SDET.', application: 'NEEDS_USER_INPUT' });
    const fine = seed(db, { title: 'Senior SDET', application: 'READY_FOR_REVIEW' });
    const sent = seed(db, { title: 'QA Intern (Summer)', application: 'SUBMITTED' });
    db.prepare("INSERT INTO questions(job_id,question,requires_user_input,status) VALUES(?,'This is an Unpaid internship, are you okay with this?',1,'OPEN')").run(intern);
    db.prepare("INSERT INTO contacts(company,name,title,linkedin_url,relevance_score) VALUES('Acme','Arnav','Recruiter','https://linkedin.com/in/arnav',80)").run();
    db.prepare("INSERT INTO outreach(job_id,contact_id,type,message,character_count,status) VALUES(?,1,'CONNECTION','Hi',2,'OUTREACH_READY')").run(intern);
    const result = retireExcludedApplications(db, config, 'r');
    assert.equal(result.belowLevel, 2);
    assert.equal(statusOf(db, intern), 'EXCLUDED'); assert.equal(statusOf(db, junior), 'EXCLUDED');
    assert.equal(statusOf(db, fine), 'READY_FOR_REVIEW'); assert.equal(statusOf(db, sent), 'SUBMITTED');
    assert.equal((db.prepare("SELECT status FROM questions WHERE job_id=?").get(intern) as { status: string }).status, 'STALE');
    assert.equal((db.prepare("SELECT status FROM outreach WHERE job_id=?").get(intern) as { status: string }).status, 'JOB_EXCLUDED');
    assert.equal(retireExcludedApplications(db, config, 'r').belowLevel, 0, 'running again changes nothing');
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

// Live 2026-09-29/30: Versatile Club reposted one role under a new LinkedIn id (the same question was asked twice), and Welo Data posted
// three roles on two career sites each.
test('two open applications to one role at one company keep the one further along; one already submitted wins outright', async () => {
  const { root, db } = await env();
  try {
    const first = seed(db, { company: 'Versatile Club Employer of Record', title: 'QA Tester – Java Automation', application: 'NEEDS_USER_INPUT', hoursAgo: 12 });
    const repost = seed(db, { company: 'Versatile Club Employer of Record', title: 'QA Tester - Java Automation', application: 'READY_FOR_REVIEW', hoursAgo: 6 });
    const applied = seed(db, { company: 'Welo Data', title: 'Manual QA Tester', application: 'ALREADY_APPLIED', hoursAgo: 20 });
    const copy = seed(db, { company: 'Welo Data', title: 'Manual QA Tester', application: 'READY_FOR_SUBMISSION', hoursAgo: 2 });
    const other = seed(db, { company: 'Welo Data', title: 'SDET - API/Middleware', application: 'FAILED' });
    const result = retireExcludedApplications(db, config, 'r');
    assert.equal(result.duplicates, 2);
    assert.equal(statusOf(db, first), 'NEEDS_USER_INPUT'); assert.equal(statusOf(db, repost), 'EXCLUDED');
    assert.equal(statusOf(db, applied), 'ALREADY_APPLIED'); assert.equal(statusOf(db, copy), 'EXCLUDED');
    assert.equal(statusOf(db, other), 'FAILED');
    assert.match((db.prepare('SELECT filter_reason FROM jobs WHERE id=?').get(repost) as { filter_reason: string }).filter_reason, new RegExp(`Duplicate of job ${first}`));
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

test('the queue never opens a second application to a role already applied to, nor one below the level', async () => {
  const { root, db } = await env();
  try {
    const original = seed(db, { title: 'Senior SDET', application: 'NEEDS_USER_INPUT' });
    const repost = seed(db, { title: 'Senior SDET' });
    const junior = seed(db, { title: 'QA Engineer', description: '1-2 years of testing experience.' });
    const fresh = seed(db, { title: 'Lead QA Engineer' });
    const result = await processEligibleJobs(root, db, config, 'r');
    assert.equal(result.queued, 1);
    const job = (id: number) => db.prepare('SELECT status,filter_reason FROM jobs WHERE id=?').get(id) as { status: string; filter_reason: string };
    assert.equal(job(repost).status, 'DUPLICATE'); assert.match(job(repost).filter_reason, new RegExp(`Duplicate of job ${original}`));
    assert.equal(job(junior).status, 'SKIPPED'); assert.match(job(junior).filter_reason, /below your minimum of 4/);
    assert.equal(statusOf(db, fresh), 'READY_FOR_REVIEW');
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

test('discovery rejects an internship outright', async () => {
  const { root, db } = await env();
  try {
    const persisted = persistJob(db, { company: 'Kalpapreeth IT Solutions', title: 'Quality Assurance Intern - Unpaid', location: 'Hyderabad, Telangana, India', workMode: 'Hybrid', jobUrl: 'https://www.linkedin.com/jobs/view/4473999999', source: 'linkedin', postedAt: new Date(Date.now() - 3600_000).toISOString(), description: 'Functional testing and regression testing for our QA team.' }, config);
    assert.equal(persisted.disposition, 'REJECTED_SEARCH'); assert.equal(persisted.reason, 'Internship or entry-level role');
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

// Live 2026-09-30 (IAssess, LinkedIn education page): "From" and "To" are each a month and a year dropdown under one label. After the
// person filled June 2016 - May 2020 and typed continue, the year was saved over the month and refilled into the month dropdown.
test('answers saved after a pause keep same-label dropdowns apart and never make them remembered facts', async () => {
  const { root, db } = await env();
  try {
    const months = [{ label: 'Month', value: '' }, { label: 'June', value: '6' }, { label: 'May', value: '5' }];
    const years = [{ label: 'Year', value: '' }, { label: '2016', value: '2016' }, { label: '2020', value: '2020' }];
    const onScreen: FormField[] = [
      { key: '0', label: 'From', type: 'select', required: false, value: '6', options: months }, { key: '1', label: 'From', type: 'select', required: false, value: '2016', options: years },
      { key: '2', label: 'To', type: 'select', required: false, value: '5', options: months }, { key: '3', label: 'To', type: 'select', required: false, value: '2020', options: years },
      { key: '4', label: 'Notes', type: 'text', required: false, value: 'a' }, { key: '5', label: 'Notes', type: 'text', required: false, value: 'b' },
      { key: '6', label: 'Degree', type: 'text', required: true, value: 'Bachelor of Engineering' },
    ];
    const answers: Record<string, CandidateAnswer> = {};
    const saved = learnVerifiedAnswers(db, 1, { fields: [], questions: onScreen.map((field) => ({ question: field.label, classification: 'MISSING', required: true })) }, onScreen, answers);
    assert.deepEqual(saved.sort(), ['Degree = Bachelor of Engineering', 'From [month] = June', 'From [year] = 2016', 'To [month] = May', 'To [year] = 2020']);
    assert.equal(classifyField({ label: 'From', type: 'select', required: false, options: months }, answers).answer, 'June');
    assert.equal(classifyField({ label: 'From', type: 'select', required: false, options: years }, answers).answer, '2016');
    assert.equal(answers.notes, undefined, 'same-label text boxes cannot be told apart and are left as the person set them');
    const remembered = (db.prepare('SELECT fact FROM answer_memory').all() as Array<{ fact: string }>).map((row) => row.fact);
    assert.deepEqual(remembered, ['degree']);
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

test('one generic title used for two different roles is not a duplicate: the descriptions must match too', async () => {
  const { root, db } = await env();
  try {
    const sap = 'We need a tester for SAP S/4HANA finance modules. You will write test scenarios for general ledger, accounts payable, asset accounting and controlling, run integration tests with MM and SD, coordinate UAT with finance users, and document defects in Solution Manager. Five years of SAP functional testing experience.';
    const web = 'We need an automation engineer for our consumer web and mobile apps. You will build Playwright and Appium suites in TypeScript, run them in GitHub Actions, review pull requests for testability, mock backend services, and report flaky tests with traces. Five years of UI automation experience with modern JavaScript frameworks.';
    const first = seed(db, { company: 'Wipro', title: 'TEST ENGINEER L3', description: sap, application: 'NEEDS_USER_INPUT' });
    const second = seed(db, { company: 'Wipro', title: 'TEST ENGINEER L3', description: web, application: 'READY_FOR_REVIEW' });
    const repost = seed(db, { company: 'Wipro', title: 'TEST ENGINEER L3', description: `${sap} Apply now.` });
    assert.equal(retireExcludedApplications(db, config, 'r').duplicates, 0);
    assert.equal(statusOf(db, first), 'NEEDS_USER_INPUT'); assert.equal(statusOf(db, second), 'READY_FOR_REVIEW');
    await processEligibleJobs(root, db, config, 'r');
    assert.equal((db.prepare('SELECT status FROM jobs WHERE id=?').get(repost) as { status: string }).status, 'DUPLICATE', 'the SAP text reposted is the SAP role');
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

// Live 2026-09-30: `review 6376` on a 29-hour-old posting did nothing and turned its open questions into "expired".
test('an explicit review of one job is attempted whatever its age; a normal run still skips postings past the freshness window', async () => {
  const { root, db } = await env();
  try {
    const cfg = structuredClone(config); cfg.application.delay_between_jobs_ms = 0;
    const noBrowser = async () => { throw new Error('no browser in this test'); };
    const reviewed = seed(db, { title: 'Senior SDET', application: 'READY_FOR_REVIEW', hoursAgo: 30 });
    await processApplicationQueue(root, db, cfg, 'r', noBrowser, reviewed);
    assert.notEqual(statusOf(db, reviewed), 'FAILED', 'the review was attempted');
    const stale = seed(db, { title: 'Lead SDET', application: 'READY_FOR_REVIEW', hoursAgo: 30 });
    await processApplicationQueue(root, db, cfg, 'r', noBrowser);
    assert.equal((db.prepare("SELECT stage FROM application_events WHERE job_id=? ORDER BY id DESC").get(stale) as { stage: string }).stage, 'EXPIRED');
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});
