import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { mkdtemp, mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium, type Browser, type Page } from 'playwright';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/database.js';
import { processApplicationPage } from '../src/applications.js';
import { processEligibleJobs } from '../src/pipeline.js';
import { processApplicationQueue } from '../src/application-runner.js';
import { buildCandidateAnswers, normalizeQuestion } from '../src/ats/answers.js';
import { LinkedInBudget } from '../src/linkedin-budget.js';
import { transitionApplication } from '../src/state.js';

let browser: Browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

const config = await loadConfig(process.cwd());
config.candidate.skill_years = undefined; // QUESTION below must stay one only the user can answer, whatever the local profile says
// Likewise the name tests check LinkedIn's own prefilled split against full_name, which only applies when no first/last name is configured.
config.candidate.first_name = undefined; config.candidate.last_name = undefined;
const JOB_URL = 'https://www.linkedin.com/jobs/view/4467318349/';
const QUESTION = 'How many years of experience do you have with Selenium?';

// A copy of the structure of LinkedIn's real Easy Apply dialog, captured from a live job page: contact selects, a phone box, a résumé card list with a hidden upload input,
// a "top choice" checkbox that must be left alone, a "follow company" checkbox that is ticked by default, and Next / Review / Submit buttons.
const clientScript = String.raw`
var CFG = __CFG__;
var step = CFG.startStep || 0;
var cards = CFG.cards.slice();
function hit(name) { fetch('/__' + name).catch(function () {}); }
function cardHtml(card, index) {
  return '<div><h3>' + card.name + '</h3><input type="radio" name="resume" id="jobsDocumentCardToggle-' + index + '"' + (card.checked ? ' checked' : '') + '><label for="jobsDocumentCardToggle-' + index + '">Select resume ' + card.name + '</label></div>';
}
function stepHtml() {
  var body = '';
  if (step === 0) {
    body = '<h3>Contact info</h3>'
      + '<div><label for="email">Email</label><span>Required</span><select id="email" required aria-required="true"><option>Select an option</option><option selected value="' + CFG.email + '">' + CFG.email + '</option></select></div>'
      + '<div><label for="cc">Phone country code</label><span>Required</span><select id="cc" required aria-required="true"><option>Select an option</option><option selected value="India (+91)">India (+91)</option><option value="Afghanistan (+93)">Afghanistan (+93)</option></select></div>'
      + '<div><label for="phone">Phone</label><input type="text" id="phone" required value="' + CFG.phone + '"></div>'
      + '<h4>Top choice job</h4><input type="checkbox" id="topchoice"><label for="topchoice">Mark job as a top choice job</label>'
      + '<div id="cards">' + cards.map(cardHtml).join('') + '</div>'
      + '<label for="file">Upload resume</label><input type="file" id="file" name="file">';
  } else if (step === 1) {
    body = '<h3>Additional questions</h3><div><label for="q1">' + CFG.question + '</label><input id="q1" type="text" required></div>'
      + (CFG.optional ? '<div><label for="q2">Are you comfortable with the hourly rate of $13.5/hr?</label><input id="q2" type="text"></div>' : '');
  } else {
    body = '<h3>Review your application</h3><p>Everything looks good</p>';
  }
  var last = CFG.multi ? step === 2 : true;
  var next = '<button type="button" id="next" aria-label="' + (step === 1 ? 'Review your application' : 'Continue to next step') + '">Next</button>';
  var submit = '<button type="button" id="submit" aria-label="Submit application">Submit application</button>';
  var back = step > 0 ? '<button type="button" id="back" aria-label="Back to previous step">Back</button>' : '';
  return '<div role="dialog"><h2 id="jobs-apply-header">Apply to Acme</h2><div role="region" aria-label="Your job application progress is at ' + (step * 50) + ' percent."><form>' + body + '</form></div>'
    + '<footer><input type="checkbox" id="follow-company-checkbox" checked><label for="follow-company-checkbox">Follow to stay up to date with their page.</label>' + back + (last ? submit : next) + '</footer></div>';
}
function wire() {
  var file = document.getElementById('file');
  if (file) file.addEventListener('change', function () {
    hit('uploaded');
    cards.forEach(function (card) { card.checked = false; });
    cards.push({ name: file.files[0].name, checked: true });
    document.getElementById('cards').innerHTML = cards.map(cardHtml).join('');
  });
  var next = document.getElementById('next');
  if (next) next.addEventListener('click', function () { step++; render(); });
  var back = document.getElementById('back');
  if (back) back.addEventListener('click', function () { step--; render(); });
  var submit = document.getElementById('submit');
  if (submit) submit.addEventListener('click', function () {
    hit('submitted');
    document.getElementById('root').innerHTML = '<div role="dialog"><h2>Application sent</h2><p>Your application was sent to Acme</p></div>';
    // LinkedIn itself does this: the confirmation is shown only briefly before a follow-up "nudge" dialog replaces it in the DOM.
    if (CFG.raceConfirmation) setTimeout(function () { document.getElementById('root').innerHTML = '<div role="dialog"><h2>Turn your resume into a profile</h2></div>'; }, 300);
  });
}
function render() { document.getElementById('root').innerHTML = stepHtml(); wire(); }
var easy = document.getElementById('easy');
if (easy) easy.addEventListener('click', function (event) { event.preventDefault(); render(); });
`;

type Fixture = { cards?: Array<{ name: string; checked: boolean }>; multi?: boolean; applied?: boolean; raceConfirmation?: boolean; startStep?: number; optional?: boolean; pageTitle?: string; externalOnly?: boolean };
function jobPage(fixture: Fixture): string {
  const phone = (config.candidate.phone.replace(/\D/g, '')).slice(-10);
  const settings = { email: config.candidate.email, phone, question: QUESTION, multi: !!fixture.multi, cards: fixture.cards ?? [{ name: 'Old_Resume_20.pdf', checked: true }], raceConfirmation: !!fixture.raceConfirmation, startStep: fixture.startStep ?? 0, optional: !!fixture.optional };
  const moreJobs = '<section><h2>More jobs</h2><a id="card" href="https://www.linkedin.com/jobs/view/999/"><span>QA Engineer (Remote)</span><span>Quik Hire Staffing</span><span>Easy Apply</span></a></section>';
  const entry = fixture.externalOnly ? `<button aria-label="Apply to Acme on company website">Apply</button>${moreJobs}` : fixture.applied ? '<p>Applied 2 days ago</p>' : '<a id="easy" aria-label="Easy Apply to this job" href="#">Easy Apply</a>';
  return `<!doctype html><html><head><title>${fixture.pageTitle ?? 'Quality Engineer'} | Acme | LinkedIn</title></head><body><h1>${fixture.pageTitle ?? 'Quality Engineer'}</h1>${entry}<div id="root"></div><script>${clientScript.replace('__CFG__', JSON.stringify(settings))}</script></body></html>`;
}

async function open(fixture: Fixture, url = JOB_URL) {
  const hits = { uploaded: 0, submitted: 0 };
  const context = await browser.newContext();
  await context.route('**/*', (route) => {
    const address = route.request().url();
    if (address.includes('/__uploaded')) { hits.uploaded++; return route.fulfill({ status: 200, body: 'ok' }); }
    if (address.includes('/__submitted')) { hits.submitted++; return route.fulfill({ status: 200, body: 'ok' }); }
    return route.fulfill({ contentType: 'text/html', body: jobPage(fixture) });
  });
  const page = await context.newPage();
  await page.goto(url);
  return { page, hits, context };
}

async function withRoot<T>(run: (root: string, resume: string) => Promise<T>): Promise<T> {
  const root = await mkdtemp(join(tmpdir(), 'job-link-easy-'));
  await mkdir(join(root, 'resumes'));
  const resume = join(root, 'resumes', 'Main.pdf');
  await writeFile(resume, '%PDF-1.4\nControlled resume\n');
  try { return await run(root, resume); } finally { await rm(root, { recursive: true, force: true }); }
}

function apply(page: Page, root: string, resumePath: string, options: { answers?: Record<string, string>; allowSubmit?: boolean; applicationUrl?: string } = {}) {
  const answers = buildCandidateAnswers(config);
  for (const [question, value] of Object.entries(options.answers ?? {})) answers[normalizeQuestion(question)] = { value, confidence: 'configured' };
  const applicationUrl = options.applicationUrl ?? JOB_URL;
  return processApplicationPage(page, root, { jobId: 1, job: { company: 'Acme', title: 'Quality Engineer', location: 'Hyderabad', workMode: 'Hybrid', jobUrl: applicationUrl, source: 'linkedin', postedAt: new Date().toISOString(), description: 'QA role', applicationUrl }, resumePath, candidateAnswers: answers },
    { allowSubmit: !!options.allowSubmit, beforeSubmit: options.allowSubmit ? async () => undefined : undefined });
}

const selectedCard = (page: Page) => page.evaluate(() => { const radio = document.querySelector('input[type=radio]:checked'); return radio ? radio.parentElement?.querySelector('h3')?.textContent ?? null : null; });

test('a one-page Easy Apply form is filled from the profile, gets the chosen resume, leaves the company unfollowed, and is never submitted without confirmation', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({});
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(result.ats, 'linkedin-easy-apply'); assert.equal(result.resumeUploaded, true); assert.equal(result.questions.length, 0);
    assert.equal(hits.uploaded, 1, 'the chosen resume was not on LinkedIn, so it is uploaded once');
    assert.equal(await selectedCard(page), 'Main.pdf');
    assert.equal(await page.locator('#follow-company-checkbox').isChecked(), false, 'applying must not also follow the company');
    assert.equal(await page.locator('#topchoice').isChecked(), false, 'the paid top-choice option is never ticked');
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});

test('a resume LinkedIn already lists (under its own numbered name) is selected instead of uploaded again', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({ cards: [{ name: 'Other.pdf', checked: true }, { name: 'Main_21.pdf', checked: false }] });
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(hits.uploaded, 0); assert.equal(await selectedCard(page), 'Main_21.pdf');
    await context.close();
  });
});

test('a different resume whose name merely starts with the chosen one is not mistaken for it', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({ cards: [{ name: 'Main_Extended.pdf', checked: true }] });
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(hits.uploaded, 1); assert.equal(await selectedCard(page), 'Main.pdf');
    await context.close();
  });
});

test('a question only the user can answer stops the walk on that page and is reported, with nothing submitted', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({ multi: true });
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'NEEDS_USER_INPUT');
    assert.ok(result.questions.some((question) => question.question === QUESTION && question.classification === 'MISSING'), JSON.stringify(result.questions));
    assert.match(await page.locator('h3').first().innerText(), /Additional questions/);
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});

// Live 2026-09-30 (Welocalize): LinkedIn reopened a saved application on its final review page; seven pages, eleven questions among them,
// were never looked at and the job was reported ready. The walk now goes Back to page 1 first, so every page gets the same checks.
test('an application LinkedIn reopens on its review page is walked from page 1, so an unanswered question on an earlier page still stops it', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({ multi: true, startStep: 2 });
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'NEEDS_USER_INPUT', result.reason);
    assert.ok(result.questions.some((question) => question.question === QUESTION), JSON.stringify(result.questions));
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});

test('optional questions left blank are named in the ready result, so they can be seen before submitting', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({ multi: true, optional: true });
    const result = await apply(page, root, resume, { answers: { [QUESTION]: '5' } });
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.match(result.reason ?? '', /optional questions left blank: Are you comfortable with the hourly rate of \$13\.5\/hr\?/);
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});

// Live 2026-09-30 (Digitrix): the employer shortened the title after posting ("…Selenium with Java" -> "…Selenium"); the job number and
// company still matched, so it is the same posting. A different role on that page is still refused.
test('a posting whose title the employer edited is still recognised by its job number and company; a different role is not', async () => {
  const context = (resumePath: string) => ({ jobId: 1, job: { company: 'Acme', title: 'QA Automation Engineer - Selenium with Java', location: 'Hyderabad', workMode: 'Hybrid', jobUrl: JOB_URL, source: 'linkedin', postedAt: new Date().toISOString(), description: 'QA role', applicationUrl: JOB_URL }, resumePath, candidateAnswers: buildCandidateAnswers(config) });
  const refused = 'Application identity could not be verified against the expected role, company and job URL';
  await withRoot(async (root, resume) => {
    const opened = await open({ multi: true, pageTitle: 'QA Automation Engineer - Selenium' });
    const result = await processApplicationPage(opened.page, root, context(resume), { allowSubmit: false });
    assert.notEqual(result.reason, refused);
    await opened.context.close();
  });
  await withRoot(async (root, resume) => {
    const opened = await open({ multi: true, pageTitle: 'Payroll Specialist' });
    const result = await processApplicationPage(opened.page, root, context(resume), { allowSubmit: false });
    assert.equal(result.reason, refused);
    assert.equal(result.fieldsFilled, 0);
    await opened.context.close();
  });
});

// Live 2026-09-30 (La Baguette): a posting that applies on the company site had no Easy Apply of its own, but its "More jobs" cards carry an
// "Easy Apply" badge, and the card for another job was clicked. Only a control whose name starts with "Easy Apply" is the job's own.
test('a posting without its own Easy Apply never clicks the Easy Apply badge of another job in "More jobs"', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({ externalOnly: true });
    const result = await apply(page, root, resume);
    assert.match(page.url(), /4467318349/, `navigated to ${page.url()}`);
    assert.equal(result.status, 'UNSUPPORTED_ATS', result.reason);
    assert.equal(result.fieldsFilled, 0); assert.equal(hits.submitted, 0);
    await context.close();
  });
});

test('a saved answer lets the walk continue through the questions page to the review page, where it stops before Submit', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({ multi: true });
    const result = await apply(page, root, resume, { answers: { [QUESTION]: '5' } });
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.match(await page.locator('h3').first().innerText(), /Review your application/);
    assert.equal(await page.getByRole('button', { name: 'Submit application' }).count(), 1); assert.equal(hits.submitted, 0);
    await context.close();
  });
});

test('with explicit confirmation the same form is submitted once, and the sent notice is recorded as the confirmation', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({});
    const result = await apply(page, root, resume, { allowSubmit: true });
    assert.equal(result.status, 'SUBMITTED', result.reason);
    assert.match(result.confirmation ?? '', /application was sent to Acme/i);
    assert.equal(hits.submitted, 1);
    await context.close();
  });
});

test('a submission that really did succeed is still recognised as SUBMITTED even when LinkedIn replaces the confirmation with a follow-up dialog moments later', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({ raceConfirmation: true });
    const result = await apply(page, root, resume, { allowSubmit: true });
    assert.equal(result.status, 'SUBMITTED', result.reason);
    assert.match(result.confirmation ?? '', /application was sent to Acme/i);
    assert.equal(hits.submitted, 1);
    await context.close();
  });
});

test('a job page for a different posting than the one queued is refused before anything is filled', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({});
    const result = await apply(page, root, resume, { applicationUrl: 'https://www.linkedin.com/jobs/view/1111111111/' });
    assert.equal(result.status, 'FAILED'); assert.match(result.reason ?? '', /identity/i);
    assert.equal(result.fieldsFilled, 0); assert.equal(hits.uploaded, 0); assert.equal(hits.submitted, 0);
    await context.close();
  });
});

test('a posting that already shows as applied is recognised, not applied to again', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await open({ applied: true });
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'ALREADY_APPLIED'); assert.equal(hits.submitted, 0);
    await context.close();
  });
});

// A separate, smaller fixture matching a common real structure (e.g. Dautom's real Easy Apply form, captured live): the résumé lives on
// its own step, separate from contact info, so a blocking field on an earlier step genuinely leaves the résumé step unreached.
// The email select is deliberately fixed to the LinkedIn account's own address ("account@linkedin.example"), which does not match
// config/candidate.json's configured email — exactly the real mismatch this test guards against being treated as an error.
function multiStepFixture(blockOnContactStep: boolean): string {
  const accountEmail = 'account@linkedin.example';
  const script = String.raw`
var step = 0;
var cards = [];
function hit(name) { fetch('/__' + name).catch(function () {}); }
function cardHtml(card, index) { return '<div><h3>' + card.name + '</h3><input type="radio" name="resume" id="jobsDocumentCardToggle-' + index + '"' + (card.checked ? ' checked' : '') + '><label for="jobsDocumentCardToggle-' + index + '">Select resume ' + card.name + '</label></div>'; }
function stepHtml() {
  var body = '';
  if (step === 0) {
    body = '<h3>Contact info</h3>'
      + '<div><label for="email">Email addressEmail address</label><select id="email" required aria-required="true"><option>Select an option</option><option selected value="${accountEmail}">${accountEmail}</option></select></div>'
      + (${blockOnContactStep} ? '<div><label for="ref">How did a colleague refer you?</label><input id="ref" type="text" required></div>' : '');
  } else if (step === 1) {
    body = '<h3>Resume</h3><div id="cards">' + cards.map(cardHtml).join('') + '</div><label for="file">Upload resume</label><input type="file" id="file" name="file">';
  } else if (step === 2) {
    body = (${!blockOnContactStep} ? '<h3>Additional Questions</h3><div><label for="q1">${QUESTION}</label><input id="q1" type="text" required></div>' : '<h3>Review your application</h3>');
  } else {
    body = '<h3>Review your application</h3>';
  }
  var next = '<button type="button" id="next" aria-label="Continue to next step">Next</button>';
  var submit = '<button type="button" id="submit" aria-label="Submit application">Submit application</button>';
  return '<div role="dialog"><h2 id="jobs-apply-header">Apply to Acme</h2><form>' + body + '</form><footer><input type="checkbox" id="follow-company-checkbox" checked>' + (step === 3 ? submit : next) + '</footer></div>';
}
function wire() {
  var file = document.getElementById('file');
  if (file) file.addEventListener('change', function () { hit('uploaded'); cards = [{ name: file.files[0].name, checked: true }]; document.getElementById('cards').innerHTML = cards.map(cardHtml).join(''); });
  var next = document.getElementById('next');
  if (next) next.addEventListener('click', function () { step++; render(); });
  var submit = document.getElementById('submit');
  if (submit) submit.addEventListener('click', function () { hit('submitted'); document.getElementById('root').innerHTML = '<div role="dialog"><h2>Application sent</h2><p>Your application was sent to Acme</p></div>'; });
}
function render() { document.getElementById('root').innerHTML = stepHtml(); wire(); }
document.getElementById('easy').addEventListener('click', function (event) { event.preventDefault(); render(); });
`;
  return `<!doctype html><html><head><title>Quality Engineer | Acme | LinkedIn</title></head><body><h1>Quality Engineer</h1><a id="easy" aria-label="Easy Apply to this job" href="#">Easy Apply</a><div id="root"></div><script>${script}</script></body></html>`;
}

test('an account email LinkedIn prefills into a fixed select is accepted even though it differs from config/candidate.json (there is no way to type a different one into that control)', async () => {
  await withRoot(async (root, resume) => {
    const context = await browser.newContext();
    await context.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: multiStepFixture(false) }));
    const page = await context.newPage();
    await page.goto(JOB_URL);
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'NEEDS_USER_INPUT', result.reason);
    assert.equal(result.resumeUploaded, true, 'the résumé step was reached and completed');
    assert.deepEqual(result.questions.map((q) => q.question), [QUESTION], 'the email mismatch is not treated as a question');
    await context.close();
  });
});

test('a required field with no known answer on an early step correctly stops the walk there and is reported as a real question, not a false "resume not attached" failure', async () => {
  await withRoot(async (root, resume) => {
    const context = await browser.newContext();
    await context.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: multiStepFixture(true) }));
    const page = await context.newPage();
    await page.goto(JOB_URL);
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'NEEDS_USER_INPUT', result.reason);
    assert.equal(result.resumeUploaded, false, 'the résumé step (one step later) was never reached');
    assert.deepEqual(result.questions.map((q) => q.question), ['How did a colleague refer you?']);
    assert.doesNotMatch(result.reason ?? '', /resume is not verifiably attached/i, 'the real, answerable reason must not be masked by the generic résumé message');
    await context.close();
  });
});

type Db = ReturnType<typeof openDatabase>;
function seedEasyApplyJob(db: Db): number {
  db.prepare("INSERT INTO runs(id,started_at,status,mode) VALUES('r',?,'STARTED','live')").run(new Date().toISOString());
  db.prepare("INSERT INTO resumes(filename,category,keywords,updated_at,available) VALUES('Main.pdf','MAIN','[]',?,1)").run(new Date().toISOString());
  return Number(db.prepare(`INSERT INTO jobs(company,title,location,job_url,source,posted_at,detected_at,description,application_url,external_id,metadata,fit_score,status,evidence_mode,updated_at)
    VALUES('Acme','Quality Engineer','Hyderabad',?,'linkedin',?,?,'QA role',?,'4467318349',?,90,'REVIEW','live',?)`).run(JOB_URL, new Date(Date.now() - 3600_000).toISOString(), new Date().toISOString(), JOB_URL, JSON.stringify({ applyKind: 'easy_apply' }), new Date().toISOString()).lastInsertRowid);
}

test('an Easy Apply job goes through the normal queue: queued (not a manual task), filled, counted against the LinkedIn page log, and held back while LinkedIn is paused', async () => {
  await withRoot(async (root) => {
    const db = openDatabase(root);
    try {
      const id = seedEasyApplyJob(db);
      const queued = await processEligibleJobs(root, db, config, 'r');
      assert.equal(queued.queued, 1); assert.equal(queued.needsUserInput, 0);
      assert.equal((db.prepare('SELECT status FROM applications WHERE job_id=?').get(id) as { status: string }).status, 'READY_FOR_REVIEW');
      const cfg = structuredClone(config); cfg.application.delay_between_jobs_ms = 0;
      // LinkedIn paused: nothing is claimed and no browser is opened.
      const budget = new LinkedInBudget(db, cfg.safety); budget.recordBlock('test pause');
      const held = await processApplicationQueue(root, db, cfg, 'r', async () => { throw new Error('a browser must not be opened while LinkedIn is paused'); });
      assert.equal(held.processed, 0);
      assert.equal((db.prepare('SELECT status FROM applications WHERE job_id=?').get(id) as { status: string }).status, 'READY_FOR_REVIEW');
      budget.clearBlock();
      const { context, hits } = await open({});
      const done = await processApplicationQueue(root, db, cfg, 'r', async () => context);
      assert.equal(done.processed, 1);
      const row = db.prepare('SELECT status, ats, resume FROM applications WHERE job_id=?').get(id) as { status: string; ats: string; resume: string };
      assert.equal(row.status, 'READY_FOR_SUBMISSION'); assert.equal(row.ats, 'linkedin-easy-apply'); assert.equal(row.resume, 'Main.pdf');
      assert.equal((db.prepare("SELECT COUNT(*) n FROM linkedin_actions WHERE kind='easy-apply'").get() as { n: number }).n, 1);
      await context.close();
    } finally { db.close(); }
  });
});

test('a question that a later attempt resolves (a saved answer, a code fix) is marked stale, not left open forever, while a still-unresolved one is left untouched', async () => {
  await withRoot(async (root) => {
    const db = openDatabase(root);
    try {
      const id = seedEasyApplyJob(db);
      await processEligibleJobs(root, db, config, 'r');
      const cfg = structuredClone(config); cfg.application.delay_between_jobs_ms = 0;
      const { context, hits } = await open({ multi: true });
      db.prepare("INSERT INTO runs(id,started_at,status,mode) VALUES('r1',?,'STARTED','live')").run(new Date().toISOString());
      await processApplicationQueue(root, db, cfg, 'r1', async () => context);
      assert.equal((db.prepare('SELECT status FROM applications WHERE job_id=?').get(id) as { status: string }).status, 'NEEDS_USER_INPUT');
      const firstOpen = db.prepare("SELECT id, question, status FROM questions WHERE job_id=?").all(id) as Array<{ id: number; question: string; status: string }>;
      assert.deepEqual(firstOpen.map((q) => [q.question, q.status]), [[QUESTION, 'OPEN']]);
      // Answer it, exactly as `npm run dev -- answer <id> <value>` would, then review again (same as `npm run dev -- review <jobId>`).
      cfg.application.answers = { [QUESTION]: '5' };
      db.prepare('UPDATE applications SET attempt_count=0 WHERE job_id=?').run(id);
      transitionApplication(db, id, 'READY_FOR_REVIEW', 'USER_REVIEW');
      db.prepare("INSERT INTO runs(id,started_at,status,mode) VALUES('r2',?,'STARTED','live')").run(new Date().toISOString());
      await processApplicationQueue(root, db, cfg, 'r2', async () => context);
      assert.equal((db.prepare('SELECT status FROM applications WHERE job_id=?').get(id) as { status: string }).status, 'READY_FOR_SUBMISSION');
      const after = db.prepare('SELECT question, status FROM questions WHERE job_id=?').all(id) as Array<{ question: string; status: string }>;
      assert.deepEqual(after, [{ question: QUESTION, status: 'STALE' }], 'the resolved question is closed, not left as if still blocking');
      assert.equal((db.prepare("SELECT COUNT(*) n FROM questions WHERE job_id=? AND status='OPEN'").get(id) as { n: number }).n, 0);
      assert.equal(hits.submitted, 0);
      await context.close();
    } finally { db.close(); }
  });
});

// The layout LinkedIn switched to on 2026-09-29, copied from a live capture (Welocalize, 1/7 pages): a native <dialog> (no role="dialog"),
// a "#dialog-header" title, "N percent complete" + "N/M pages" progress instead of an aria-labelled region, fields labelled by
// <label for> around a <div>, First/Last name prefilled from the member's profile, and buttons named only by their text ("Next").
// The résumé and review steps are modelled on the same conventions (the live capture could only see page 1 without filling it).
type NativeFixture = { lastName?: string; cards?: Array<{ name: string; checked: boolean }>; unknownButton?: boolean; loaderMs?: number; progressbar?: boolean };
function nativeDialogFixture(fixture: NativeFixture = {}): string {
  const words = config.candidate.full_name.trim().split(/\s+/);
  const settings = { first: words.slice(0, -1).join(' '), last: fixture.lastName ?? words[words.length - 1], cards: fixture.cards ?? [{ name: 'Old_Resume.pdf', checked: true }], unknownButton: !!fixture.unknownButton, loaderMs: fixture.loaderMs ?? 0, progressbar: !!fixture.progressbar };
  const script = String.raw`
var CFG = __CFG__;
var step = 0;
var cards = CFG.cards.slice();
function hit(name) { fetch('/__' + name).catch(function () {}); }
function field(id, label, control) { return '<div componentkey="easyApplyFieldFocus_' + id + '"><label for="' + id + '"><div>' + label + '</div></label><div>' + control + '</div></div>'; }
function card(item, index) { return '<div><input type="radio" name="doc" id="_r_c' + index + '_" aria-label="Select this resume"' + (item.checked ? ' checked' : '') + '><div><p>' + item.name + '</p><p>Last used on 9/22/2026</p></div></div>'; }
function stepHtml() {
  var body = '';
  if (step === 0) {
    body = '<p>Contact info</p>'
      + field('_r_l_', 'First name*', '<input required id="_r_l_" dir="auto" type="text" value="' + CFG.first + '">')
      + field('_r_m_', 'Last name*', '<input required id="_r_m_" dir="auto" type="text" value="' + CFG.last + '">')
      + field('_r_n_', 'Phone country code*', '<select id="_r_n_" required><option value="" disabled hidden></option><option value="af">Afghanistan (+93)</option><option value="in" selected>India (+91)</option></select>')
      + field('_r_p_', 'Mobile phone number*', '<input required id="_r_p_" dir="auto" type="tel" value="">')
      + field('_r_q_', 'Email address*', '<select id="_r_q_" required><option value="" disabled hidden></option><option value="account@linkedin.example" selected>account@linkedin.example</option></select>');
  } else if (step === 1) {
    body = '<p>Resume</p><div id="cards">' + cards.map(card).join('') + '</div><label for="_r_u_"><span>Upload resume</span></label><input type="file" id="_r_u_" style="display:none">';
  } else {
    body = '<p>Review your application</p><input type="checkbox" id="_r_f_" checked><label for="_r_f_">Follow Acme to stay up to date with their page.</label>';
  }
  var label = step === 2 ? 'Submit application' : (CFG.unknownButton ? 'Proceed' : 'Next');
  return '<dialog open><div><header id="dialog-header"><h2>Apply to Acme</h2></header><div data-testid="dialog-content"><div>' + Math.round((step + 1) * 100 / 3) + ' percent complete</div><p>' + (step + 1) + '/3 pages</p>'
    + (CFG.progressbar ? '<div role="progressbar" aria-valuenow="33"></div>' : '') + body + '<div id="error"></div><footer><button type="button" id="primary"><span><span>' + label + '</span></span></button></footer></div></div></dialog>';
}
function wire() {
  var file = document.getElementById('_r_u_');
  if (file) file.addEventListener('change', function () {
    hit('uploaded');
    cards.forEach(function (item) { item.checked = false; });
    cards.push({ name: file.files[0].name, checked: true });
    document.getElementById('cards').innerHTML = cards.map(card).join('');
  });
  document.getElementById('primary').addEventListener('click', function () {
    if (step === 2) {
      hit('submitted');
      document.getElementById('root').innerHTML = '<dialog open><h2>Application sent</h2><p>Your application was sent to Acme</p></dialog>';
      return;
    }
    // Like the real form: Next does nothing but show an error until every required field on the page has a value.
    var empty = Array.prototype.some.call(document.querySelectorAll('dialog [required]'), function (control) { return !control.value; });
    if (empty) { document.getElementById('error').innerHTML = '<p role="alert">Please enter a valid answer</p>'; return; }
    step++; load();
  });
}
function render() { document.getElementById('root').innerHTML = stepHtml(); wire(); }
// Like the live dialog: the title shows at once, the page itself only after a spinner.
function load() {
  if (!CFG.loaderMs) return render();
  document.getElementById('root').innerHTML = '<dialog open><button type="button" aria-label="Dismiss"></button><div><header id="dialog-header"><h2>Apply to Acme</h2></header><div data-testid="dialog-content"><div data-testid="loader"></div></div></div></dialog>';
  setTimeout(render, CFG.loaderMs);
}
document.getElementById('easy').addEventListener('click', function () { load(); });
`;
  return `<!doctype html><html><head><title>Quality Engineer | Acme | LinkedIn</title></head><body><h1>Quality Engineer</h1><button id="easy" aria-label="Easy Apply to this job"><span>Easy Apply</span></button><div id="root"></div><script>${script.replace('__CFG__', JSON.stringify(settings))}</script></body></html>`;
}

async function openNative(fixture: NativeFixture = {}) {
  const hits = { uploaded: 0, submitted: 0 };
  const context = await browser.newContext();
  await context.route('**/*', (route) => {
    const address = route.request().url();
    if (address.includes('/__uploaded')) { hits.uploaded++; return route.fulfill({ status: 200, body: 'ok' }); }
    if (address.includes('/__submitted')) { hits.submitted++; return route.fulfill({ status: 200, body: 'ok' }); }
    return route.fulfill({ contentType: 'text/html', body: nativeDialogFixture(fixture) });
  });
  const page = await context.newPage();
  await page.goto(JOB_URL);
  return { page, hits, context };
}

test('the native-<dialog> layout (live since 2026-09-29) is walked like the old one: profile name accepted, phone filled, resume attached, company unfollowed, stopped before Submit', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openNative();
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(result.ats, 'linkedin-easy-apply'); assert.equal(result.resumeUploaded, true); assert.deepEqual(result.questions, []);
    assert.equal(hits.uploaded, 1, 'the chosen resume was not listed, so it is uploaded once');
    assert.match(await page.locator('dialog').innerText(), /3\/3 pages[\s\S]*Review your application/);
    assert.equal(await page.locator('#_r_f_').isChecked(), false, 'applying must not also follow the company');
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});

test('in the native-<dialog> layout a resume LinkedIn already lists is picked by its file name even though its radio has no <label for>', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openNative({ cards: [{ name: 'Other.pdf', checked: true }, { name: 'Main_3.pdf', checked: false }] });
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(hits.uploaded, 0);
    await context.close();
  });
});

test('in the native-<dialog> layout, confirmation submits once and the sent notice is the recorded confirmation', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openNative();
    const result = await apply(page, root, resume, { allowSubmit: true });
    assert.equal(result.status, 'SUBMITTED', result.reason);
    assert.match(result.confirmation ?? '', /application was sent to Acme/i);
    assert.equal(hits.submitted, 1);
    await context.close();
  });
});

test('a prefilled last name that does not read back as the configured full name is a question, not silently accepted', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openNative({ lastName: 'Somebodyelse' });
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'NEEDS_USER_INPUT', result.reason);
    assert.deepEqual(result.questions.map((q) => q.question), ['Last name*']);
    assert.equal(hits.uploaded, 0); assert.equal(hits.submitted, 0);
    await context.close();
  });
});

test('a step with no recognisable Next/Submit button stops with a reason naming the page, and the dialog HTML is kept beside the screenshot', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openNative({ unknownButton: true });
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'VALIDATION_FAILURE', result.reason);
    assert.match(result.reason ?? '', /Easy Apply stopped at .*1\/3 pages.*no Next, Review or Submit button/);
    const saved = (await readdir(join(root, 'screenshots'))).filter((name) => name.endsWith('_dialog.html'));
    assert.equal(saved.length, 1);
    assert.match(await readFile(join(root, 'screenshots', saved[0]), 'utf8'), /Apply to Acme[\s\S]*Proceed/);
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});

// Live 2026-09-29 (job 6579): the dialog's title appeared at once but its form only after a spinner, and reading it at the title found
// no fields, so the job went to "apply manually" and the browser closed within two seconds. Every page here loads behind a spinner.
test('a dialog that shows its title first and its form only after a loading spinner is waited for, on opening and after each Next', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openNative({ loaderMs: 1500 });
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(result.resumeUploaded, true);
    assert.match(await page.locator('dialog').innerText(), /3\/3 pages[\s\S]*Review your application/);
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});

// A second native-<dialog> shape, copied from two live snapshots saved on 2026-09-29 (Codefeast, Versatile Club): everything on one page.
// The résumé list sits under the contact details as a radiogroup whose radios are named only by aria-label (their <label for> is empty),
// there is no file input (an "Upload resume" button opens the file picker), the follow checkbox's words are in its aria-label, and a
// "Location (city)*" typeahead is titled by a <p> rather than a <label>, is not marked required, and offers role="option" suggestions.
type SinglePageFixture = { cards?: Array<{ name: string; checked: boolean }>; location?: boolean; cities?: string[]; refuseNext?: boolean };
function singlePageFixture(fixture: SinglePageFixture = {}): string {
  const phone = config.candidate.phone.replace(/\D/g, '').slice(-10);
  const settings = { phone, cards: fixture.cards ?? [{ name: 'Other.pdf', checked: true }], location: !!fixture.location, cities: fixture.cities ?? ['Hyderabad, Telangana, India', 'Hyderabad, Sindh, Pakistan'], refuseNext: !!fixture.refuseNext };
  const script = String.raw`
var CFG = __CFG__;
var cards = CFG.cards.slice();
function hit(name) { fetch('/__' + name).catch(function () {}); }
function card(item, index) {
  return '<div><div role="button" tabindex="0"><p>PDF</p><p><span>' + item.name + '</span></p><p>9/22/2026</p></div>'
    + '<div aria-label="' + item.name + '"><input id="_r_c' + index + '_" aria-label="' + item.name + '" type="radio" name="radio-group-_r_k_"' + (item.checked ? ' checked' : '') + '><label for="_r_c' + index + '_"></label></div></div>';
}
function html() {
  return '<dialog data-testid="dialog" aria-labelledby="dialog-header" open><button type="button" aria-label="Dismiss"></button><div><header id="dialog-header"><h2>Apply to Acme</h2></header><div data-testid="dialog-content">'
    + '<p>Contact info</p>'
    + '<div><label for="_r_e_"><div>Email*</div></label><div><select id="_r_e_" required><option value="" disabled hidden></option><option value="account@linkedin.example" selected>account@linkedin.example</option></select></div></div>'
    + '<div><label for="_r_g_"><div>Phone country code*</div></label><div><select id="_r_g_" required><option value="" disabled hidden></option><option value="in" selected>India (+91)</option></select></div></div>'
    + '<div><label for="_r_i_"><div>Phone*</div></label><div><input required id="_r_i_" type="tel" value="' + CFG.phone + '"></div></div>'
    + (CFG.location ? '<div><p>Location (city)*</p><div role="status"></div><div><div><input id="_r_o_" data-testid="typeahead-input" autocomplete="off" placeholder="Enter city or location" aria-autocomplete="list" value=""></div></div><div id="results"></div></div>' : '')
    + '<div><h3>Top choice job</h3><input id="_r_j_" aria-label="Mark job as a top choice" type="checkbox"><label for="_r_j_"></label></div>'
    + '<div><p>Resume</p><fieldset role="radiogroup"><div id="cards">' + cards.map(card).join('') + '</div></fieldset><button type="button" id="upload"><span><span>Upload resume</span></span></button></div>'
    + '<div><input id="_r_q_" aria-label="Follow Acme to stay up to date with their page" type="checkbox" checked><label for="_r_q_"></label><p>Follow Acme to stay up to date with their page</p></div>'
    + '<div id="error"></div><footer><button type="button" id="primary"><span><span>' + (CFG.refuseNext ? 'Next' : 'Submit application') + '</span></span></button></footer></div></div></dialog>';
}
function wire() {
  document.getElementById('upload').addEventListener('click', function () {
    var input = document.createElement('input'); input.type = 'file'; input.style.display = 'none';
    input.addEventListener('change', function () {
      hit('uploaded');
      cards.forEach(function (item) { item.checked = false; });
      cards.push({ name: input.files[0].name, checked: true });
      document.getElementById('cards').innerHTML = cards.map(card).join('');
    });
    document.body.appendChild(input); input.click();
  });
  var city = document.getElementById('_r_o_');
  if (city) city.addEventListener('input', function () {
    var shown = /^hyd/i.test(city.value) ? CFG.cities : [];
    document.getElementById('results').innerHTML = shown.length ? '<div role="listbox">' + shown.map(function (name) { return '<div role="option">' + name + '</div>'; }).join('') + '</div>' : '';
    Array.prototype.forEach.call(document.querySelectorAll('#results [role=option]'), function (option) {
      option.addEventListener('click', function () { city.value = option.textContent; document.getElementById('results').innerHTML = ''; });
    });
  });
  document.getElementById('primary').addEventListener('click', function () {
    if (CFG.refuseNext || (city && !city.value)) { document.getElementById('error').innerHTML = '<p>This field is required</p>'; return; }
    hit('submitted');
    document.getElementById('root').innerHTML = '<dialog open><h2>Application sent</h2><p>Your application was sent to Acme</p></dialog>';
  });
}
document.getElementById('easy').addEventListener('click', function () { document.getElementById('root').innerHTML = html(); wire(); });
`;
  return `<!doctype html><html><head><title>Quality Engineer | Acme | LinkedIn</title></head><body><h1>Quality Engineer</h1><button id="easy" aria-label="Easy Apply to this job"><span>Easy Apply</span></button><div id="root"></div><script>${script.replace('__CFG__', JSON.stringify(settings))}</script></body></html>`;
}

async function openSinglePage(fixture: SinglePageFixture = {}) {
  const hits = { uploaded: 0, submitted: 0 };
  const context = await browser.newContext();
  await context.route('**/*', (route) => {
    const address = route.request().url();
    if (address.includes('/__uploaded')) { hits.uploaded++; return route.fulfill({ status: 200, body: 'ok' }); }
    if (address.includes('/__submitted')) { hits.submitted++; return route.fulfill({ status: 200, body: 'ok' }); }
    return route.fulfill({ contentType: 'text/html', body: singlePageFixture(fixture) });
  });
  const page = await context.newPage();
  await page.goto(JOB_URL);
  return { page, hits, context };
}

test('a résumé list on the contact page with aria-label-only radios gets the chosen résumé selected (no upload), the company unfollowed and "top choice" left alone', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openSinglePage({ cards: [{ name: 'Other.pdf', checked: true }, { name: 'Main.pdf', checked: false }] });
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(result.resumeUploaded, true); assert.equal(hits.uploaded, 0);
    assert.equal(await page.getByRole('radio', { name: 'Main.pdf' }).isChecked(), true);
    assert.equal(await page.locator('#_r_q_').isChecked(), false, 'applying must not also follow the company');
    assert.equal(await page.locator('#_r_j_').isChecked(), false, 'the paid top-choice option is never ticked');
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});

test('with no file input, a résumé LinkedIn does not have yet is uploaded through the "Upload resume" button and then selected', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openSinglePage();
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(hits.uploaded, 1);
    assert.equal(await page.getByRole('radio', { name: 'Main.pdf' }).isChecked(), true);
    await context.close();
  });
});

test('a "Location (city)*" typeahead titled by plain text is recognised, typed by its first part, and the one suggestion matching every part is chosen', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openSinglePage({ cards: [{ name: 'Main.pdf', checked: true }], location: true });
    const result = await apply(page, root, resume, { answers: { 'Location (city)': 'Hyderabad, India' } });
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(await page.locator('#_r_o_').inputValue(), 'Hyderabad, Telangana, India');
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});

// Live 2026-09-29 (Versatile Club): LinkedIn listed "Hyderabad, Telangana, India" seven times (distinct option ids, identical text).
// Identical wording is one choice to anyone reading it, so it is not ambiguity; a different place that also matches still would be.
test('a city typeahead that repeats the same suggestion several times still picks it; two different matching places stay a question', async () => {
  await withRoot(async (root, resume) => {
    const live = ['Hyderabad, Telangana, India', 'Greater Hyderabad Area', 'Hyderabad, Sindh, Pakistan', 'Hyderabad District, Sindh, Pakistan', 'Hyderabad, Telangana, India', 'Hyderabad, Telangana, India'];
    const { page, hits, context } = await openSinglePage({ cards: [{ name: 'Main.pdf', checked: true }], location: true, cities: live });
    const result = await apply(page, root, resume, { answers: { 'Location (city)': 'Hyderabad, India' } });
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(await page.locator('#_r_o_').inputValue(), 'Hyderabad, Telangana, India');
    assert.equal(hits.submitted, 0);
    await context.close();
  });
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openSinglePage({ cards: [{ name: 'Main.pdf', checked: true }], location: true, cities: ['Hyderabad, Andhra Pradesh, India', 'Hyderabad, Telangana, India'] });
    const result = await apply(page, root, resume, { answers: { 'Location (city)': 'Hyderabad, India' } });
    assert.equal(result.status, 'NEEDS_USER_INPUT', result.reason);
    assert.match(result.reason ?? '', /unambiguous/);
    assert.deepEqual(result.questions.map((question) => question.question), ['Location (city)*']);
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});

test('when LinkedIn will not leave a page after Next, the walk stops at once with what LinkedIn shows, instead of refilling the page a dozen times', async () => {
  await withRoot(async (root, resume) => {
    const { page, hits, context } = await openSinglePage({ cards: [{ name: 'Main.pdf', checked: true }], refuseNext: true });
    const started = Date.now();
    const result = await apply(page, root, resume);
    assert.equal(result.status, 'VALIDATION_FAILURE', result.reason);
    assert.match(result.reason ?? '', /LinkedIn stayed on .* after Next \("This field is required"\)/);
    assert.ok(result.fieldsFilled < 10, `filled ${result.fieldsFilled} fields: the page was refilled over and over`);
    assert.ok(Date.now() - started < 30_000, `took ${Date.now() - started} ms`);
    assert.equal(hits.submitted, 0);
    await context.close();
  });
});


test('a persistent LinkedIn progress bar is not mistaken for a loading spinner; the phone is filled', async () => {
  await withRoot(async (root,resume)=>{
    const {page,context}=await openNative({progressbar:true});
    const result=await apply(page,root,resume);
    assert.equal(result.status,'READY_FOR_SUBMISSION',result.reason);
    assert.equal(await page.locator('#_r_p_').count(),0,'the contact step advanced after the phone was filled');
    assert.equal(result.fieldsFilled>=1,true);
    await context.close();
  });
});
