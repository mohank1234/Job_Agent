import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig } from '../src/config.js';
import { scoreJob } from '../src/fit.js';
import { scanResumes, selectResume } from '../src/resumes.js';
import { buildConnectionNote, buildInMail, validateMessage } from '../src/outreach.js';
import { prepareOutreachForJobs, rankContacts } from '../src/outreach-framework.js';
import { canonicalContactUrl, parseStructuredContacts } from '../src/contact-sources.js';
import { openDatabase } from '../src/database.js';

const config = await loadConfig(process.cwd());
const goodDescription = 'QA Engineer with 5+ years experience. Required: manual testing, functional testing, Selenium, Java and API testing. Build regression testing and automation testing for our Hyderabad team.';

test('verified skills and overall experience produce explainable strong fit', () => {
  const fit = scoreJob(goodDescription, config, { title: 'QA Engineer', location: 'Hyderabad' });
  assert.ok(fit.score >= 75); assert.equal(fit.decision, 'ELIGIBLE'); assert.ok(fit.matched.includes('Java')); assert.equal(fit.mandatoryMissing.length, 0);
});
test('mandatory Python is not satisfied by Java and caps eligibility', () => {
  const fit = scoreJob(`${goodDescription}\nPython is mandatory.`, config, { title: 'QA Engineer', location: 'Hyderabad' });
  assert.ok(fit.mandatoryMissing.includes('Python')); assert.ok(fit.score < 65); assert.equal(fit.decision, 'NOT_ELIGIBLE');
});
test('preferred gap stays preferred even when unrelated requirements exist', () => {
  const fit = scoreJob(`${goodDescription}\nKubernetes is preferred.`, config, { title: 'QA Engineer', location: 'Hyderabad' });
  assert.ok(fit.preferredMissing.includes('Kubernetes')); assert.equal(fit.mandatoryMissing.length, 0); assert.ok(fit.score >= 75);
});
test('mixed mandatory and preferred clauses keep independent requirement meaning', () => {
  for (const sentence of ['Python is required, Kubernetes preferred.', 'Python is required and Kubernetes is preferred.', 'Required: Python, preferred: Kubernetes.']) {
    const fit = scoreJob(`${goodDescription}\n${sentence}`, config, {title:'QA Engineer', location:'Hyderabad'});
    assert.ok(fit.mandatoryMissing.includes('Python'), sentence);
    assert.ok(fit.preferredMissing.includes('Kubernetes'), sentence);
    assert.ok(!fit.mandatoryMissing.includes('Kubernetes'), sentence);
  }
});
test('specific tool duration and senior duration must be verified independently', () => {
  assert.ok(scoreJob(`${goodDescription}\nRequired: 3 years of experience with Playwright.`, config, {title:'QA Engineer',location:'Hyderabad'}).mandatoryMissing.some((gap) => gap.includes('specified technology')));
  assert.ok(scoreJob(`${goodDescription}\nAt least 8 years experience.`, config, {title:'Senior QA Engineer',location:'Hyderabad'}).mandatoryMissing.includes('8+ years overall experience'));
});
test('required alternative is satisfied without claiming unverified Python', () => {
  const fit = scoreJob(`${goodDescription}\nRequired: Java or Python.`, config, {title:'QA Engineer',location:'Hyderabad'});
  assert.ok(!fit.matched.includes('Python')); assert.ok(!fit.mandatoryMissing.includes('Python'));
});
test('profile omissions remain unverified even if PDF has those skills', () => {
  const fit = scoreJob(`${goodDescription}\nSQL, JavaScript and Jenkins are required.`, config, {title:'QA Engineer',location:'Hyderabad'});
  for (const name of ['SQL','JavaScript','Jenkins']) assert.ok(fit.mandatoryMissing.includes(name));
});
test('bare authorization Yes does not prove US authorization', () => {
  const fit = scoreJob(`${goodDescription}\nMust be authorized to work in the United States.`, config, {title:'QA Engineer',location:'Remote'});
  assert.ok(fit.mandatoryMissing.some((gap) => gap.includes('Work authorization')));
});
test('explicit country authorization is respected and Scrum Master is not a degree', () => {
  const verified = structuredClone(config); verified.candidate.authorized_countries = ['US'];
  const fit = scoreJob(`${goodDescription}\nMust be authorized to work in the United States.\nRequired to collaborate with the Scrum Master.`, verified, {title:'QA Engineer',location:'Remote'});
  assert.ok(!fit.mandatoryMissing.some((gap) => /authorization|postgraduate/i.test(gap)));
});
test('sparse keyword list cannot become high fit', () => {
  assert.ok(scoreJob('QA Selenium Java', config, {title:'QA Engineer',location:'Hyderabad'}).score < 65);
});

function pdf(text: string): Buffer {
  const stream = `BT /F1 12 Tf 20 750 Td (${text}) Tj ET`;
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>', '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>', '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`];
  let value = '%PDF-1.4\n'; const offsets = [0];
  for (const [i, object] of objects.entries()) { offsets.push(Buffer.byteLength(value)); value += `${i+1} 0 obj\n${object}\nendobj\n`; }
  const start = Buffer.byteLength(value); value += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map((offset) => `${String(offset).padStart(10,'0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
  return Buffer.from(value);
}
test('rescan extracts contents, excludes removed files, selects AI main and Java senior', async () => {
  const root = await mkdtemp(join(tmpdir(),'job-link-resumes-')); const db = openDatabase(root);
  try {
    await mkdir(join(root,'resumes')); await mkdir(join(root,'config'));
    await writeFile(join(root,'resumes','Resume_Main.pdf'), pdf('QA Engineer LLM testing RAG testing Promptfoo Playwright'));
    await writeFile(join(root,'resumes','Resume_Senior.pdf'), pdf('Senior QA Engineer Selenium Java API testing TestNG'));
    const records = await scanResumes(root, db); assert.equal(records.length,2); assert.ok(records.every((row) => row.contentHash && !row.extractionError));
    assert.equal(selectResume(db,'Senior AI QA Engineer','LLM testing RAG testing Promptfoo')?.category,'MAIN');
    assert.equal(selectResume(db,'Senior QA Engineer','Selenium Java API testing TestNG')?.category,'SENIOR_QA');
    await rm(join(root,'resumes','Resume_Senior.pdf')); await scanResumes(root,db);
    assert.equal(selectResume(db,'Senior QA Engineer','Selenium Java')?.category,'MAIN');
    assert.equal((db.prepare('SELECT available FROM resumes WHERE filename=?').get('Resume_Senior.pdf') as {available:number}).available,0);
  } finally { db.close(); await rm(root,{recursive:true,force:true}); }
});
test('connection notes fit limit naturally and applied statement requires actual submission', () => {
  const input = {firstName:'Asha',company:'An Example Company',title:'Senior QA Engineer',candidateName:'Test',specialization:'LLM testing'};
  const note = buildConnectionNote(input,300); validateMessage(note,300); assert.ok(!note.includes('applied'));
  assert.ok(buildInMail({...input,submitted:true}).includes('applied'));
  const short = buildConnectionNote({...input,title:'Extremely long job title '.repeat(40)},150); assert.ok([...short].length<=150);
  assert.throws(() => validateMessage('🧪'.repeat(301),300));
});
test('structured contact sources require explicit name, title and URL', () => {
  const html = '<script type="application/ld+json">{"@graph":[{"@type":"Person","name":"Asha","jobTitle":"Technical Recruiter","sameAs":"https://www.linkedin.com/in/test-recruiter"},{"@type":"Person","name":"Unspecified"}]}</script>';
  const contacts = parseStructuredContacts(html,'Test Company'); assert.equal(contacts.length,1); assert.equal(rankContacts(contacts)[0].relevanceScore,95);
  assert.equal(canonicalContactUrl('https://www.linkedin.com/in/test/?trk=foo#abc'),'https://linkedin.com/in/test');
});
test('outreach is durable, repeat-safe, company cooldown-aware, and never sends', async () => {
  const root = await mkdtemp(join(tmpdir(),'job-link-outreach-')); const db=openDatabase(root);
  try {
    await mkdir(join(root,'config'));
    await writeFile(join(root,'config','contacts.json'),JSON.stringify({contacts:[{company:'Test Company',name:'Asha',title:'Technical Recruiter',profileUrl:'https://www.linkedin.com/in/fixture-contact',relevanceScore:0}],sources:[]}));
    const add = db.prepare("INSERT INTO jobs(company,title,location,job_url,source,detected_at,description,fit_score,status) VALUES('Test Company','QA Engineer','Hyderabad',?,'fixture',?,'QA Engineer',90,'APPLICATION_READY')");
    add.run('https://example.com/1',new Date().toISOString()); add.run('https://example.com/2',new Date().toISOString());
    const first = await prepareOutreachForJobs(root,db,config,'test-run',[1]); assert.equal(first.prepared,1); assert.equal(first.completed,0);
    assert.equal((await prepareOutreachForJobs(root,db,config,'test-run-2',[1,2])).prepared,0);
    const row = db.prepare('SELECT status, message, sent_at FROM outreach').get() as {status:string;message:string;sent_at:string|null};
    assert.equal(row.status,'OUTREACH_READY'); assert.equal(row.sent_at,null); assert.ok(!row.message.includes('applied'));
  } finally { db.close(); await rm(root,{recursive:true,force:true}); }
});
