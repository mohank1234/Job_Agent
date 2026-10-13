import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/database.js';
import { inboxCounts } from '../src/inbox.js';

const config = await loadConfig(process.cwd());

test('the inbox counts what waits on the person: ready applications, manual ones, and open questions grouped by job', async () => {
  const root = await mkdtemp(join(tmpdir(), 'job-link-inbox-'));
  const db = openDatabase(root);
  try {
    const now = new Date().toISOString();
    const job = (n: number, status = 'REVIEW'): number => Number(db.prepare("INSERT INTO jobs(company,title,location,job_url,source,detected_at,description,fit_score,status,evidence_mode,posted_at,updated_at) VALUES('Acme',?,'Hyderabad',?,'linkedin',?,'QA',80,?,'live',?,?)").run(`Role ${n}`, `https://example.com/j/${n}`, now, status, now, now).lastInsertRowid);
    const app = (jobId: number, status: string): void => { db.prepare("INSERT INTO applications(job_id,application_url,status,updated_at) VALUES(?,?,?,?)").run(jobId, `https://example.com/apply/${jobId}`, status, now); };
    const question = (jobId: number, text: string, category?: string, field?: string): void => { db.prepare("INSERT INTO questions(job_id,question,requires_user_input,status,category,field_key) VALUES(?,?,1,'OPEN',?,?)").run(jobId, text, category ?? null, field ?? null); };
    const a = job(1); app(a, 'READY_FOR_SUBMISSION');
    const b = job(2); app(b, 'NEEDS_USER_INPUT'); question(b, 'Notice period?'); question(b, 'Expected CTC?');
    const c = job(3); app(c, 'NEEDS_USER_INPUT'); question(c, 'Apply on the employer page', 'MANUAL_APPLY');
    const d = job(4, 'SUBMITTED'); app(d, 'SUBMITTED'); question(d, 'Old question');
    const e = job(5); app(e, 'NEEDS_USER_INPUT'); question(e, 'Confirm submission', undefined, 'submission_confirmation');
    assert.deepEqual(inboxCounts(db, config), { ready: 1, manual: 1, questions: 2, questionJobs: 1 });
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

test('number selections accept lists, ranges, all and none', async () => {
  const { parseSelection } = await import('../src/inbox.js');
  assert.deepEqual([...parseSelection('1,3,5-7', 10)], [1, 3, 5, 6, 7]);
  assert.deepEqual([...parseSelection('all', 3)], [1, 2, 3]);
  assert.deepEqual([...parseSelection('', 5)], []);
  assert.deepEqual([...parseSelection('none', 5)], []);
  assert.deepEqual([...parseSelection('2, 9, 4-12', 5)], [2, 4, 5], 'numbers beyond the list are ignored');
  assert.deepEqual([...parseSelection('x, 0, -3', 5)], []);
});

test('typed answers become what a form expects, and are saved for the next application', async () => {
  const { normalizeTypedAnswer, rememberAnswer, isSensitiveQuestion, loadAnswerMemory } = await import('../src/answer-memory.js');
  assert.equal(normalizeTypedAnswer('Do you have experience with Playwright?', 'y'), 'Yes');
  assert.equal(normalizeTypedAnswer('Have you worked on API testing', 'n'), 'No');
  assert.equal(normalizeTypedAnswer('Notice period (days)', 'y'), 'y', 'a y on a non yes-no question is left alone');
  assert.equal(normalizeTypedAnswer('How many years with Selenium?', ' 5 '), '5');
  assert.equal(isSensitiveQuestion('Are you legally authorized to work in the US?'), true);
  assert.equal(isSensitiveQuestion('I agree to the privacy policy'), true);
  assert.equal(isSensitiveQuestion('Do you have experience with CI/CD pipelines?'), false);
  const root = await mkdtemp(join(tmpdir(), 'job-link-answers-'));
  const db = openDatabase(root);
  try {
    assert.equal(rememberAnswer(db, 'Do you have experience with CI/CD pipelines?', 'Yes'), 'question');
    assert.equal(rememberAnswer(db, 'Are you authorized to work in the US?', 'No'), 'not-reused');
    assert.equal(rememberAnswer(db, 'What is your notice period?', '30'), 'fact');
    const answers: Record<string, { value: string; confidence: 'verified' | 'configured' }> = {};
    loadAnswerMemory(db, answers);
    const { normalizeQuestion } = await import('../src/ats/answers.js');
    assert.equal(answers[normalizeQuestion('Do you have experience with CI/CD pipelines?')]?.value, 'Yes', 'the same question is answered automatically next time');
    assert.equal(answers[normalizeQuestion('Are you authorized to work in the US?')], undefined, 'legal questions are never reused');
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});
