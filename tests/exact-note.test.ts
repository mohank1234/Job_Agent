import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildExactConnectionNote, type Audience } from '../src/outreach.js';
import { jdRelevantSkills } from '../src/outreach-framework.js';

const names = ['Vedika', 'Al', 'Venkata Satyanarayana', 'Priyanka', 'Zoe', 'Bhargavi'];
const companies = ['NationsBenefits', 'IBM', 'Welocalize', 'Tata Consultancy Services', 'Krishu Global Pvt Ltd', 'ICE', 'Persistent Systems', 'JPMorgan Chase & Co.'];
const titles = ['Quality Engineer', 'Senior SDET', 'QA Automation Engineer', 'Senior Software Development Engineer in Test (SDET)', 'Test Lead - Hyderabad', 'AI QA Engineer'];
const skillSets = [['Playwright'], ['Playwright', 'Selenium'], ['Selenium', 'Playwright', 'Java'], ['API Testing', 'Selenium', 'Playwright', 'Java'], ['LLM Testing', 'RAG Testing', 'Promptfoo']];

test('every connection note is exactly 300 characters: message, one blank line, then the signature', () => {
  let count = 0;
  for (const firstName of names) for (const company of companies) for (const title of titles) for (const skills of skillSets) for (const audience of ['HR', 'MANAGER', 'EXEC'] as Audience[]) for (const submitted of [false, true]) {
    const note = buildExactConnectionNote({ firstName, company, title, signature: 'Test Candidate', skills, years: 5, audience, submitted });
    assert.equal([...note].length, 300, note); assert.ok(note.endsWith('.\n\nTest Candidate') || note.endsWith('\n\nTest Candidate'), note);
    assert.equal(note.split('\n').length, 3, 'exactly one blank line before the signature'); assert.ok(note.includes(company)); count++;
  }
  assert.ok(count > 5000);
});

test('the skills in a note are the ones the posting asks for, and only skills from the profile', () => {
  const candidate = { skills: ['Manual Testing', 'Selenium', 'Playwright', 'Java', 'Quality Assurance', 'API Testing'], automation_skills: ['Selenium', 'Playwright', 'Java'], ai_testing_skills: ['LLM Testing'] } as never;
  assert.deepEqual(jdRelevantSkills('Quality Engineer', 'You will build Playwright suites in Java and test REST APIs. Playwright experience is a must. We use Kubernetes and SQL.', candidate), ['Playwright', 'Java', 'API Testing']);
  assert.deepEqual(jdRelevantSkills('Chef', 'Cook meals', candidate), []);
});
