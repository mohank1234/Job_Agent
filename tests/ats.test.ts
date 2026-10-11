import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium, type Browser, type Page } from 'playwright';
import { processApplicationPage } from '../src/applications.js';
import { buildCandidateAnswers, classifyField } from '../src/ats/answers.js';
import { matchingSelectOption } from '../src/ats/form.js';
import { defaultAtsRegistry } from '../src/ats/index.js';
import { detectSafetyStop, detectSecurityText } from '../src/safety.js';
import { loadConfig } from '../src/config.js';
import type { ApplicationContext } from '../src/core/types.js';

let browser: Browser;
let root: string;
let resume: string;
before(async () => {
  root = await mkdtemp(join(tmpdir(), 'job-link-ats-'));
  resume = join(root, 'candidate.pdf');
  await writeFile(resume, '%PDF-1.4\nControlled test resume\n');
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); await rm(root, { recursive: true, force: true }); });

const body = (extra = '') => `<!doctype html><html><head><title>Acme Careers</title></head><body><h1>Quality Engineer</h1><h2>Acme</h2><form id="application-form"><label>Full name<input name="name" required></label><label>Email<input name="email" type="email" required></label><label>Resume<input type="file" name="resume" accept="application/pdf" required></label>${extra}<button type="submit">Submit application</button></form><script>document.querySelector('form').addEventListener('submit',e=>{e.preventDefault();document.querySelector('form').remove();document.body.insertAdjacentHTML('beforeend','<div role="status">Your application has been submitted.</div>')})</script></body></html>`;
async function setup(html: string, host = 'jobs.lever.co'): Promise<{ page: Page; context: ApplicationContext }> {
  const page = await browser.newPage();
  page.setDefaultTimeout(5000);
  const url = `https://${host}/acme/test-job/apply`;
  await page.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: html }));
  await page.goto(url);
  return { page, context: { jobId: 1, job: { company: 'Acme', title: 'Quality Engineer', location: 'Remote', workMode: 'Remote', jobUrl: url, applicationUrl: url, source: 'CONTROLLED TEST', externalId: 'test-job', postedAt: new Date().toISOString(), description: '' }, resumePath: resume, candidateAnswers: { 'full name': { value: 'Known Candidate', confidence: 'verified' }, email: { value: 'candidate@example.com', confidence: 'verified' } } } };
}

test('ATS registry recognizes supported hosted families and generic fallback', () => {
  for (const [url, expected] of [['https://boards.greenhouse.io/acme/jobs/1', 'greenhouse'], ['https://jobs.lever.co/acme/1', 'lever'], ['https://jobs.ashbyhq.com/acme/1', 'ashby'], ['https://acme.wd5.myworkdayjobs.com/en-US/jobs/1', 'workday'], ['https://example.com/jobs/1', 'generic']]) assert.equal(defaultAtsRegistry().forUrl(url)?.name, expected);
});

test('candidate answer builder preserves factual data and does not invent legal names or scoped authorization', async () => {
  const config = await loadConfig(process.cwd());
  config.candidate.first_name = undefined; config.candidate.last_name = undefined; // a profile that states only a full name
  const answers = buildCandidateAnswers(config);
  assert.equal(answers['full name'].value, config.candidate.full_name);
  assert.equal(answers['first name'], undefined);
  assert.equal(classifyField({ label: 'Are you authorized to work in the United States?', name: 'work_authorization', required: true }, answers).classification, 'SENSITIVE');
  assert.equal(classifyField({ label: 'Gender', required: false }, answers).classification, 'SENSITIVE');
  assert.equal(classifyField({ label: 'What is your Python experience?', name: 'years_experience', required: true }, answers).classification, 'MISSING');
});

test('known fields and selected resume are filled, policy leaves application ready', async () => {
  const { page, context } = await setup(body('<label>Preferred location<select name="location" required><option value="">Select</option><option value="remote">Remote</option></select></label>'));
  context.candidateAnswers['preferred location'] = { value: 'Remote', confidence: 'verified' };
  const result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
  assert.equal(result.fieldsFilled, 3);
  assert.equal(result.resumeUploaded, true);
  assert.equal(result.submissionAttempted, false);
  assert.equal(await page.locator('input[name="name"]').inputValue(), 'Known Candidate');
  await page.close();
});

test('unknown required questions and consent are recorded without guesses', async () => {
  const { page, context } = await setup(body('<label>Expected start date<input name="start" required></label><label>I agree to the privacy policy<input name="consent" type="checkbox" required></label><label>Gender<select name="gender"><option value="">Choose</option><option>Other</option></select></label>'));
  const result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.status, 'NEEDS_USER_INPUT', result.reason);
  assert.deepEqual(result.questions.map((question) => question.question), ['Expected start date', 'I agree to the privacy policy']);
  assert.equal(await page.locator('[name="gender"]').inputValue(), '');
  assert.equal(await page.locator('[name="consent"]').isChecked(), false);
  await page.close();
});

test('unknown radio questions stay unanswered and configured choices are checked', async () => {
  const { page, context } = await setup(body('<fieldset><legend>Available for remote work?</legend><label>Yes<input type="radio" name="remote" value="yes" required></label><label>No<input type="radio" name="remote" value="no"></label></fieldset>'));
  context.candidateAnswers['available for remote work'] = { value: 'Yes', confidence: 'configured' };
  const result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
  assert.equal(await page.locator('[value="yes"]').isChecked(), true);
  await page.close();
});

test('security text distinguishes a job describing CAPTCHA from an active challenge', () => {
  assert.equal(detectSecurityText('You will test CAPTCHA integration and use MFA in our QA projects'), undefined);
  assert.ok(detectSecurityText('Please verify you are human'));
  assert.ok(detectSecurityText('Enter the verification code sent to your phone'));
});

test('security and login stops happen before personal information is filled', async () => {
  for (const [extra, status] of [['<p>Please verify you are human</p>', 'BLOCKED_SECURITY'], ['<label>Password<input type="password"></label>', 'LOGIN_REQUIRED']]) {
    const { page, context } = await setup(body(extra));
    const result = await processApplicationPage(page, root, context, { allowSubmit: false });
    assert.equal(result.status, status);
    assert.equal(result.fieldsFilled, 0);
    assert.equal(await page.locator('[name="name"]').inputValue(), '');
    await page.close();
  }
});

test('missing resume and wrong application identity fail explicitly', async () => {
  const { page, context } = await setup(body());
  context.resumePath = join(root, 'missing.pdf');
  const missing = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(missing.status, 'FAILED');
  assert.match(missing.reason!, /Resume upload failed/);
  context.job.company = 'Different Company';
  const wrong = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(wrong.status, 'FAILED');
  assert.match(wrong.reason!, /identity/);
  await page.close();
});

test('no form cannot be declared ready', async () => {
  const { page, context } = await setup('<h1>Quality Engineer</h1><h2>Acme</h2><p>This job has closed.</p>');
  const result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.status, 'CLOSED_JOB');
  assert.equal(result.formsInspected, 0);
  await page.close();
});

test('submission requires a durable guard and records actual confirmation', async () => {
  const { page, context } = await setup(body());
  const unguarded = await processApplicationPage(page, root, context, { allowSubmit: true });
  assert.equal(unguarded.status, 'READY_FOR_SUBMISSION');
  assert.equal(unguarded.submissionAttempted, false);
  let claims = 0;
  const result = await processApplicationPage(page, root, context, { allowSubmit: true, beforeSubmit: async () => { claims++; } });
  assert.equal(claims, 1);
  assert.equal(result.status, 'SUBMITTED', result.reason);
  assert.match(result.confirmation!, /submitted/);
  assert.equal(result.submissionAttempted, true);
  await page.close();
});

test('a guard rejection never clicks submit', async () => {
  const { page, context } = await setup(body());
  const result = await processApplicationPage(page, root, context, { allowSubmit: true, beforeSubmit: async () => { throw new Error('Already submitted'); } });
  assert.equal(result.submissionAttempted, false);
  assert.equal(result.status, 'FAILED');
  assert.equal(await page.locator('form').count(), 1);
  await page.close();
});

test('Greenhouse, Ashby and generic HTML forms fill through their own profiles', async () => {
  for (const [host, ats] of [['boards.greenhouse.io', 'greenhouse'], ['jobs.ashbyhq.com', 'ashby'], ['careers.acme.test', 'generic']]) {
    const { page, context } = await setup(body(), host);
    context.job.atsJobId = `${ats}:acme:test-job`;
    const result = await processApplicationPage(page, root, context, { allowSubmit: false });
    assert.equal(result.ats, ats);
    assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
    assert.equal(result.resumeUploaded, true);
    await page.close();
  }
});

// Greenhouse's current job boards (seen live 2026-09-29, SonicWall): picking a file removes the <input type="file"> at once and shows a
// "<filename> ×" chip. The upload had worked, but waiting on the vanished input timed out and the job was reported as a failed upload.
test('an upload control that is replaced by a filename chip right after the file is chosen still counts as the resume attached', async () => {
  const chip = `<script>document.querySelector('input[type=file]').addEventListener('change',e=>{const name=e.target.files[0].name;e.target.closest('label').outerHTML='<div class="file-chip"><span>'+name+'</span><button type="button" aria-label="Remove file">×</button></div>'})</script>`;
  const { page, context } = await setup(body().replace('</body>', `${chip}</body>`), 'job-boards.greenhouse.io');
  context.job.atsJobId = 'greenhouse:acme:test-job';
  const result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.ats, 'greenhouse');
  assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
  assert.equal(result.resumeUploaded, true);
  assert.equal(await page.locator('input[type=file]').count(), 0, 'the fixture really did remove the input');
  await page.close();
});

test('an unconfirmed click is never reported submitted', async () => {
  const { page, context } = await setup(body().replace('Your application has been submitted.', 'An unexpected response occurred.'));
  const result = await processApplicationPage(page, root, context, { allowSubmit: true, beforeSubmit: async () => undefined });
  assert.equal(result.status, 'SUBMISSION_UNCONFIRMED', result.reason);
  assert.equal(result.submissionAttempted, true);
  assert.equal(result.confirmation, undefined);
  assert.match(result.reason!, /unconfirmed/);
  await page.close();
});

test('exact sensitive answers can be configured but missing legal names remain unknown', async () => {
  const field = { label: 'Are you authorized to work in India?', required: true };
  assert.equal(classifyField(field, { 'are you authorized to work in india': { value: 'Yes', confidence: 'configured' } }).answer, 'Yes');
  const { page, context } = await setup(body('<label>First name<input name="first_name" required></label><label>Last name<input name="last_name" required></label>'));
  const result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.status, 'NEEDS_USER_INPUT');
  assert.deepEqual(result.questions.map((item) => item.question), ['First name', 'Last name']);
  await page.close();
});

test('resume MIME mismatch fails before attaching an unsupported file', async () => {
  const { page, context } = await setup(body().replace('application/pdf', '.docx'));
  const result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.status, 'FAILED');
  assert.match(result.reason!, /not accepted/);
  assert.equal(result.resumeUploaded, false);
  await page.close();
});

test('semantic resume labels support anonymous ATS file names without confusing cover letters', async () => {
  const { page, context } = await setup(body('<label>Cover letter<input type="file" name="other-file"></label>').replace('name="resume"', 'name="random-uuid"'), 'jobs.ashbyhq.com');
  const result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
  assert.equal(result.resumeUploaded, true);
  assert.equal(await page.locator('[name="other-file"]').inputValue(), '');
  await page.close();
});

test('visible CAPTCHA stops the form while a passive invisible badge does not imply a challenge', async () => {
  for (const [widget, status] of [['<div data-sitekey="test">Security verification</div>', 'BLOCKED_SECURITY'], ['<div class="grecaptcha-badge" data-sitekey="test" data-size="invisible">Protected by reCAPTCHA</div>', 'READY_FOR_SUBMISSION']]) {
    const { page, context } = await setup(body(widget));
    const result = await processApplicationPage(page, root, context, { allowSubmit: false });
    assert.equal(result.status, status, result.reason);
    await page.close();
  }
});

// Live 2026-09-29: a tracking iframe on a LinkedIn company page never finished loading, and the security check's locator.count() in that
// frame waited for it forever: `find-contacts` sat silent for 25 minutes until Ctrl+C.
test('a stalled iframe that never finishes loading cannot hang the security check, and the page itself is still checked', async () => {
  const page = await browser.newPage();
  await page.route('**/stuck*', () => undefined); // never answered, exactly like the stalled frame
  await page.route('**/company', (route) => route.fulfill({ contentType: 'text/html', body: '<body><h1>Acme</h1><iframe src="https://ads.example/stuck"></iframe></body>' }));
  await page.goto('https://www.linkedin.test/company', { waitUntil: 'domcontentloaded' });
  const started = Date.now();
  assert.equal((await detectSafetyStop(page, root, 'stalled', 500)).stopped, false);
  assert.ok(Date.now() - started < 5000, `took ${Date.now() - started} ms`);
  await page.evaluate(() => document.body.insertAdjacentHTML('beforeend', '<p>Please verify you are a human</p>'));
  assert.equal((await detectSafetyStop(page, root, 'stalled', 500)).stopped, true, 'a real challenge on the page still stops everything');
  await page.close();
});

// Real wordings from one day's Easy Apply forms (2026-09-29). Each maps onto a fact already in the candidate profile, converting only exact
// units; questions the profile cannot answer, and yes/no questions, stay unanswered.
test('common screening questions in their many wordings are answered from the profile, and nothing else is guessed', async () => {
  const config = structuredClone(await loadConfig(process.cwd()));
  Object.assign(config.candidate, { years_experience: 5, notice_period: '90 days', current_salary: '13 LPA', expected_salary: '18 LPA', linkedin: 'https://www.linkedin.com/in/someone/', preferred_locations: ['Hyderabad', 'Remote'], skill_years: { Selenium: 5, 'API Testing': 4, 'QA Automation': 5 } });
  const answers = buildCandidateAnswers(config);
  const answer = (label: string, type = 'text') => { const field = classifyField({ label, required: true, type }, answers); return field.classification === 'KNOWN' ? field.answer : undefined; };
  assert.equal(answer('What is your current notice period?'), '90 days');
  assert.equal(answer('Please enter your notice period in days'), '90');
  assert.equal(answer('Notice Period in days:'), '90');
  assert.equal(answer('What is your current CTC? (Numeric - LPA)'), '13');
  assert.equal(answer('Please enter your current ctc in INR'), '1300000');
  assert.equal(answer('Please enter your expected ctc in INR'), '1800000');
  assert.equal(answer('What is your desired Compensation ( in LPA)'), '18');
  assert.equal(answer('How many years of overall work experience do you have?'), '5');
  assert.equal(answer('How Many Years of work experience do you have in your chosen engineering field*'), '5');
  assert.equal(answer('How many years of Information Technology experience do you currently have?'), '5');
  assert.equal(answer('Please select your total years of professional experience:*', 'select'), '5');
  assert.equal(answer('Please enter your LinkedIn profile URL'), 'https://www.linkedin.com/in/someone/');
  assert.equal(answer('What is your preferred working location?*'), 'Hyderabad, Remote');
  assert.equal(answer('How many years of work experience do you have with Selenium?'), '5');
  assert.equal(answer('How many years of work experience do you have with API Testing?'), '4');
  assert.equal(answer('How many years do you have with QA automation?'), '5');
  // Not in the profile, or not a value question: left for the user.
  assert.equal(answer('How many years of work experience do you have with Tricentis Tosca?'), undefined);
  assert.equal(answer('Please select your total additional months of experience:*'), undefined);
  assert.equal(answer('Monthly expected rate in INR (As per Gov. 10% TDS would be deducted):'), undefined);
  assert.equal(answer('Do you have 5+ years of experience developing applications in Python?*'), undefined);
  assert.equal(answer('Are you comfortable with a 30 days notice period?'), undefined);
  assert.equal(answer('How many years of hands-on experience do you have designing and developing automation frameworks from scratch?'), undefined);
  assert.equal(answer('Please enter your notice period in days', 'radio'), undefined, 'a choice control is never answered by inference');
  // LinkedIn's required marker after a question mark ("?*") must not stop a saved answer typed without it from matching.
  const saved = buildCandidateAnswers({ ...config, application: { ...config.application, answers: { 'What is your level of proficiency in English?': 'Professional' } } });
  assert.equal(classifyField({ label: 'What is your level of proficiency in English?*', required: true, type: 'select' }, saved).answer, 'Professional');
});

// Live 2026-09-29 (SonicWall, Greenhouse): the city typeahead offers "Hyderabad, Telangana, India", never the profile's "Hyderabad, India".
test('a Greenhouse location combobox picks the one suggestion containing every part of the configured city, and the fuller wording then validates', async () => {
  const combo = `<label for="loc">Location (City)</label><input id="loc" role="combobox" aria-autocomplete="list" required><div id="menu"></div>
<script>const loc=document.getElementById('loc');loc.addEventListener('input',()=>{const names=/^hyd/i.test(loc.value)?['Hyderabad, Telangana, India','Hyderabad, Andhra Pradesh, India','Hyderabad, Sindh, Pakistan']:[];document.getElementById('menu').innerHTML=names.map(n=>'<div role="option">'+n+'</div>').join('');document.querySelectorAll('#menu [role=option]').forEach(o=>o.addEventListener('click',()=>{loc.value=o.textContent;document.getElementById('menu').innerHTML='';}))})</script>`;
  // Live the same day, the list also held "Hyderabad, Andhra Pradesh, India": "Hyderabad, India" then names two places, so only a
  // configured location that names the state is filled; the bare city stays a question rather than a guess between the two.
  for (const [configured, expected] of [['Hyderabad, Telangana, India', 'Hyderabad, Telangana, India'], ['Hyderabad, India', '']] as const) {
    const { page, context } = await setup(body(combo), 'job-boards.greenhouse.io');
    context.job.atsJobId = 'greenhouse:acme:test-job';
    context.candidateAnswers['location (city)'] = { value: configured, confidence: 'verified' };
    const result = await processApplicationPage(page, root, context, { allowSubmit: false });
    assert.equal(result.status, expected ? 'READY_FOR_SUBMISSION' : 'NEEDS_USER_INPUT', result.reason);
    if (!expected) assert.deepEqual(result.questions.map((question) => question.question), ['Location (City)']);
    assert.equal(await page.locator('#loc').inputValue(), expected);
    await page.close();
  }
});


test('closed notices stop before Apply, even if a form is present', async () => {
  for(const notice of ['Not currently accepting applications','No longer accepting applications','Applications are closed','This job is no longer available','Position has been filled','Job expired','Posting removed','This role is closed','Job no longer exists','Requisition closed']) {
    const {page,context}=await setup(body()+`<p>${notice}</p>`);
    const result=await processApplicationPage(page,root,context,{allowSubmit:false});
    assert.equal(result.status,'CLOSED_JOB',notice);
    assert.equal(result.fieldsFilled,0);assert.deepEqual(result.questions,[]);
    await page.close();
  }
});

test('generic employer Apply navigation waits for a delayed form', async () => {
  const html='<h1>Quality Engineer</h1><h2>Acme</h2><button id="apply">Apply Now</button><script>document.querySelector("button").onclick=()=>{document.querySelector("button").remove();setTimeout(()=>document.body.insertAdjacentHTML("beforeend",'+JSON.stringify(body().replace(/<script>[\s\S]*?<\/script>/g,''))+'),200)}</script>';
  const {page,context}=await setup(html,'careers.acme.test');
  const result=await processApplicationPage(page,root,context,{allowSubmit:false});
  assert.equal(result.status,'READY_FOR_SUBMISSION',result.reason);await page.close();
});

test('known invalid values are validation failures, not candidate questions', async () => {
  const {page,context}=await setup(body('<label>Years of experience<input required type="number" min="10"></label>'));
  context.candidateAnswers['years of experience']={value:'2',confidence:'verified'};
  const result=await processApplicationPage(page,root,context,{allowSubmit:false});
  assert.equal(result.status,'VALIDATION_FAILURE',result.reason);assert.equal(result.questions.length,0);await page.close();
});

test('manual pause yields an untouched form and continues on the same page', async () => {
  const {page,context}=await setup(body('<label>Total experience<input id="years" required></label>'));
  let pauses=0;
  const result=await processApplicationPage(page,root,context,{allowSubmit:false,onUserInput:async stopped=>{
    pauses++;assert.equal(stopped.questions[0].question,'Total experience');
    const before=await page.locator('input[name=email]').inputValue();
    await page.locator('#years').fill('7');
    assert.equal(await page.locator('input[name=email]').inputValue(),before);
    context.candidateAnswers['total experience']={value:'7',confidence:'configured'};
    return true;
  }});
  assert.equal(pauses,1);assert.equal(result.status,'READY_FOR_SUBMISSION',result.reason);await page.close();
});

test('Workday advances information and questions to review without submission', async () => {
  const html=body().replace('<form id="application-form">','<form id="application-form" data-automation-id="applyFlowPage">')
    .replace('<button type="submit">Submit application</button>','<button type="button" id="next">Save and Continue</button>')
    + `<script>document.querySelector('#next').onclick=()=>{document.querySelector('form').innerHTML='<h3>Review</h3><button type="button" data-automation-id="bottom-navigation-next-button">Submit</button>'}</script>`;
  const {page,context}=await setup(html,'acme.myworkdayjobs.com');
  const result=await processApplicationPage(page,root,context,{allowSubmit:false});
  assert.equal(result.status,'READY_FOR_SUBMISSION',result.reason);assert.equal(result.submissionAttempted,false);await page.close();
});


test('generic application form inside an employer iframe is filled with parent job identity', async () => {
  const page=await browser.newPage();
  const url='https://careers.acme.test/acme/test-job/apply';
  await page.route('**/*',route=>{
    const html=route.request().url().endsWith('/embedded')?body():`<h1>Quality Engineer</h1><h2>Acme</h2><iframe src="/embedded"></iframe>`;
    return route.fulfill({contentType:'text/html',body:html});
  });
  await page.goto(url);
  const context:ApplicationContext={jobId:1,job:{company:'Acme',title:'Quality Engineer',location:'Remote',workMode:'Remote',jobUrl:url,applicationUrl:url,source:'TEST',externalId:'test-job',postedAt:new Date().toISOString(),description:''},resumePath:resume,candidateAnswers:{'full name':{value:'Known Candidate',confidence:'verified'},email:{value:'candidate@example.com',confidence:'verified'}}};
  const result=await processApplicationPage(page,root,context,{allowSubmit:false});
  assert.equal(result.status,'READY_FOR_SUBMISSION',result.reason);assert.equal(result.resumeUploaded,true);
  await page.close();
});


test('verified years and preferred locations select only one justified option',()=>{
  const years={label:'Years of experience',required:true,type:'select',answer:'5',options:[{label:'2,4',value:'a'},{label:'4,6',value:'b'},{label:'6,8',value:'c'}]};
  assert.equal(matchingSelectOption(years)?.label,'4,6');
  assert.equal(matchingSelectOption({...years,answer:'6'}),undefined,'overlapping boundary remains ambiguous');
  const location={label:'Preferred working location',required:true,type:'select',answer:'Hyderabad, Remote',options:[{label:'Chennai',value:'a'},{label:'Bangaluru',value:'b'},{label:'Hyderabad',value:'c'}]};
  assert.equal(matchingSelectOption(location)?.label,'Hyderabad');
  assert.equal(matchingSelectOption({...location,options:[{label:'Hyderabad',value:'a'},{label:'Remote',value:'b'}]}),undefined);
});


test('common phone labels use the verified profile number without matching country code',async()=>{
  const config=await loadConfig(process.cwd());
  const answers=buildCandidateAnswers(config);
  for(const label of ['Mobile phone number','Cell phone number','Contact number','Telephone number'])
    assert.equal(classifyField({label,required:true,type:'tel'},answers).answer,config.candidate.phone);
  assert.equal(classifyField({label:'Phone country code',required:true,type:'select'},answers).answer,undefined);
});


test('hidden required inputs behind a phone widget do not become duplicate phone questions',async()=>{
  const html=body('<input required tabindex="-1" aria-hidden="true" value=""><label>Mobile phone number<input type="tel" required></label>');
  const {page,context}=await setup(html);
  const config=await loadConfig(process.cwd());
  context.candidateAnswers['mobile phone number']={value:config.candidate.phone,confidence:'verified'};
  const result=await processApplicationPage(page,root,context,{allowSubmit:false});
  assert.equal(result.status,'READY_FOR_SUBMISSION',result.reason);
  assert.equal(await page.locator('input[type="tel"]').inputValue(),config.candidate.phone);
  assert.equal(result.questions.length,0);await page.close();
});

test('explicit India in the configured location answers a country field',async()=>{
  const config=await loadConfig(process.cwd());
  const answers=buildCandidateAnswers({...config,candidate:{...config.candidate,country:undefined,location:'Hyderabad, India'}});
  assert.equal(classifyField({label:'Country',required:true,type:'combobox'},answers).answer,'India');
});


test('React-style autocomplete that opens only on keystrokes chooses the unique country',async()=>{
  const combo=`<label for="country">Country</label><input id="country" role="combobox" aria-autocomplete="list" required><div id="choices"></div><script>
  const c=document.querySelector('#country');c.addEventListener('keydown',()=>{setTimeout(()=>{document.querySelector('#choices').innerHTML='<div role="option">British Indian Ocean Territory +246</div><div role="option">India +91</div><div role="option">Indonesia +62</div>';document.querySelectorAll('[role=option]').forEach(o=>o.onclick=()=>{c.value=o.textContent.split(' +')[0];document.querySelector('#choices').innerHTML=''})},0)})</script>`;
  const {page,context}=await setup(body(combo));
  context.candidateAnswers.country={value:'India',confidence:'verified'};
  const result=await processApplicationPage(page,root,context,{allowSubmit:false});
  assert.equal(result.status,'READY_FOR_SUBMISSION',result.reason);
  assert.equal(await page.locator('#country').inputValue(),'India');await page.close();
});


test('formatted phone digits validate against the configured number',async()=>{
  const {page,context}=await setup(body('<label>Phone<input type="tel" id="formatted" required></label><script>document.querySelector("#formatted").addEventListener("input",e=>{const d=e.target.value.replace(/\D/g,"");if(d.length===10)e.target.value=d.slice(0,5)+" "+d.slice(5)})</script>'));
  const config=await loadConfig(process.cwd());context.candidateAnswers.phone={value:config.candidate.phone,confidence:'verified'};
  const result=await processApplicationPage(page,root,context,{allowSubmit:false});
  assert.equal(result.status,'READY_FOR_SUBMISSION',result.reason);await page.close();
});

// Live 2026-09-29 (ValGenesis, LinkedIn): the "*" sat only in a <p> before the radiogroup; the radios themselves had no required
// attribute, an aria-label without the "*", an empty <label for>, and their visible option text in a sibling <p>.
test('a radio group whose required marker is only in the text before it is a required question with its visible option names', async () => {
  const option = (id: string, text: string) => `<div><div><input id="${id}" aria-label="What area have you most  experience in?" type="radio" name="radio-group-_r_v_"><label for="${id}"></label></div><div><p>${text}</p></div></div>`;
  const group = `<div><p>What area have you most  experience in?*</p><fieldset role="radiogroup">${option('a', 'AI/ML')}${option('b', 'Mobile')}${option('c', 'Testing')}</fieldset></div>`;
  let { page, context } = await setup(body(group));
  let result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.status, 'NEEDS_USER_INPUT', result.reason);
  assert.deepEqual(result.questions.map((question) => [question.question, question.options]), [['What area have you most  experience in?', ['AI/ML', 'Mobile', 'Testing']]]);
  await page.close();
  ({ page, context } = await setup(body(group)));
  context.candidateAnswers['what area have you most experience in'] = { value: 'Testing', confidence: 'configured' };
  result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.status, 'READY_FOR_SUBMISSION', result.reason);
  assert.equal(await page.locator('#c').isChecked(), true);
  await page.close();
});

// Live 2026-09-30: Recooty (role in an h3 under an h1 of the company), Manatal (role in an h4, company shown as the parent "Welocalize"
// for a "Welo Data" posting) and Infor (role only in the page title) application forms were all refused although they were the right job.
test('the role is found in an h3/h4 or the page title, but only whole in one place; a parent brand is accepted only on the linked host', async () => {
  const verify = async (html: string, host?: string, company?: string) => {
    const { page, context } = await setup(html, host);
    if (company) context.job.company = company;
    const verified = await defaultAtsRegistry().forPage(page)!.verifyIdentity(page, context);
    await page.close();
    return verified;
  };
  const heading = '<h1>Quality Engineer</h1><h2>Acme</h2>';
  assert.equal(await verify(body().replace(heading, '<h1>Acme</h1><h4>Quality Engineer</h4>'), 'careers.example.test'), true);
  assert.equal(await verify(body().replace(heading, '<h1>Apply</h1>').replace('<title>Acme Careers</title>', '<title>New Application | Quality Engineer | Acme Careers</title>'), 'careers.example.test'), true);
  assert.equal(await verify(body().replace(heading, '<h1>Apply</h1><h3>Quality</h3><h4>Engineer</h4>'), 'careers.example.test'), false, 'a role split across headings is not that role');
  assert.equal(await verify(body().replace(heading, '<h1>Welocalize</h1><h4>Quality Engineer</h4>'), 'welocalize-4.careers-page.com', 'Welo Data'), true);
  assert.equal(await verify(body().replace(heading, '<h1>Welocalize</h1><h4>Quality Engineer</h4>'), 'welocalize-4.careers-page.com', 'Initech'), false, 'an unrelated company is still refused');
});

// Live 2026-09-30 (Infosys): Apply opened a sign-in dialog, the next click timed out behind it, and the job was queued for pointless retries.
test('a click that times out behind a sign-in dialog is a login wall, not a retryable technical failure', async () => {
  const html = `<!doctype html><html><head><title>Acme Careers</title></head><body><h1>Quality Engineer</h1><h2>Acme</h2><button id="apply">Apply now</button>
<script>document.getElementById('apply').onclick=()=>document.body.insertAdjacentHTML('beforeend','<div style="position:fixed;inset:0;background:#fff"><h2>Log In</h2><label>Email ID<input type="email"></label><label>Password<input type="password"></label><button>Login</button></div>')</script></body></html>`;
  const { page, context } = await setup(html, 'careers.acme.test');
  const result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(result.status, 'LOGIN_REQUIRED', result.reason);
  assert.equal(result.questions.length, 0);
  await page.close();
});

// A copy of the structure of Manatal's application form (Welocalize careers, live 2026-09-30): sentence-style labels, a phone box whose
// country picker defaulted to the US and formatted a 10-digit Indian number as "(889) 740-4807", a hidden file input inside a "Resume"
// widget, and a styled terms checkbox that keeps "Apply now" disabled.
test('a Manatal-style form: dialling code typed into a country-picker phone box, résumé found in its widget, terms box asked of the person', async () => {
  const html = `<!doctype html><html><head><title>Apply now | Acme</title></head><body><h1>Quality Engineer</h1><h2>Acme</h2><form>
<div><div><label for="fn"><p>What is your full name?<span>*</span></p></label></div><div><input id="fn" placeholder="Your full name*" required></div></div>
<div data-testid="m-attachment-container"><input style="display:none" accept=".pdf,.doc,.docx" type="file"><div><div><label for="att"><p>Resume</p></label></div><div><span>Select the attachment</span><button type="button" id="att">Browse</button></div></div></div>
<div><div><label><p>What is your phone number?</p></label></div><div class="PhoneInput"><div><button type="button" id="flag">US</button></div><div><input id="ph" type="phone" placeholder="Enter a phone number"></div></div></div>
<div><div><label><p>What is your email address?</p></label></div><div><input id="em" placeholder="olivia@manatal.com"></div></div>
<div><button type="button" role="checkbox" aria-checked="false" id="agree"></button><input aria-hidden="true" tabindex="-1" type="checkbox" style="position:absolute;opacity:0"><span>I agree to the terms and conditions &amp; privacy policy</span></div>
<button type="submit" id="go" disabled>Submit application</button></form>
<script>
const ph=document.getElementById('ph');
ph.addEventListener('input',()=>{const d=ph.value.replace(/[^0-9]/g,'');if(ph.value.startsWith('+91')){document.getElementById('flag').textContent='IN';if(d.length===12)ph.value='+91 '+d.slice(2,7)+' '+d.slice(7);}else if(d.length===10){ph.value='('+d.slice(0,3)+') '+d.slice(3,6)+'-'+d.slice(6);}});
document.getElementById('agree').onclick=e=>{e.target.setAttribute('aria-checked','true');document.getElementById('go').disabled=false;};
</script></body></html>`;
  const { page, context } = await setup(html, 'careers.acme.test');
  context.candidateAnswers.phone = { value: '9000000000', confidence: 'verified' };
  context.candidateAnswers['fact:phone_dial_code'] = { value: '+91', confidence: 'verified' };
  const result = await processApplicationPage(page, root, context, { allowSubmit: false });
  assert.equal(await page.locator('#ph').inputValue(), '+91 90000 00000');
  assert.equal(await page.locator('#flag').textContent(), 'IN');
  assert.equal(await page.locator('#fn').inputValue(), 'Known Candidate');
  assert.equal(await page.locator('#em').inputValue(), 'candidate@example.com');
  assert.equal(result.resumeUploaded, true, result.reason);
  assert.equal(result.status, 'NEEDS_USER_INPUT', result.reason);
  assert.deepEqual(result.questions.map((question) => [question.question, question.classification]), [['I agree to the terms and conditions & privacy policy', 'SENSITIVE']]);
  assert.equal(await page.locator('#agree').getAttribute('aria-checked'), 'false', 'consent is never ticked for the person');
  await page.close();
});
