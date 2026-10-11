import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium, type Browser, type Route } from 'playwright';
import { parsePersonCard, searchCompanyPeople } from '../src/linkedin-people-search.js';
import { discoverAndExportContacts, exportContactsCsv } from '../src/contacts-export.js';
import { openDatabase } from '../src/database.js';

let browser: Browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

// LinkedIn's real pages, reduced to the parts the search reads (captured from live pages): a company search, a company page whose "employees" link carries the company's
// internal number, and people results as role="listitem" cards showing name, degree, headline, location, an action and a "Current:" line.
const personCard = (vanity: string, name: string, headline: string, action = 'Connect') => `<div role="listitem"><a href="https://www.linkedin.com/in/${vanity}?miniProfileUrn=urn%3Ali%3Afs_miniProfile%3AX"><span>${name}</span></a><div><span> • 2nd</span></div><div>${headline}</div><div>Hyderabad, India</div>${action === 'Connect' ? `<a aria-label="Invite ${name} to connect" href="/preload/search-custom-invite/?vanityName=${vanity}"><span>Connect</span></a>` : `<div>${action}</div>`}<div>Current: ${headline}</div></div>`;
const searchResultsHtml = `<div role="list">${personCard('ceo-jane', 'Jane Doe', 'Chief Executive Officer at Acme')}${personCard('qa-manager-bob', 'Bob Smith', 'QA Manager at Acme')}${personCard('random-eng', 'Random Engineer', 'Software Engineer at Acme')}<div role="listitem"><a href="https://www.linkedin.com/in/no-headline"><span>No Headline Person</span></a></div>${personCard('already-pending', 'Pending Pat', 'Recruiter at Acme', 'Pending')}${personCard('ceo-jane', 'Jane Doe', 'Chief Executive Officer at Acme')}</div>`;

type Hits = { companySearch: number; companyPaths: string[]; people: string[] };
const newHits = (): Hits => ({ companySearch: 0, companyPaths: [], people: [] });
function linkedinFake(hits: Hits) {
  return (route: Route) => {
    const url = new URL(route.request().url());
    const html = (body: string) => route.fulfill({ contentType: 'text/html; charset=utf-8', body: `<!doctype html><html><body>${body}</body></html>` });
    if (url.pathname.startsWith('/search/results/companies')) { hits.companySearch++; return html('<a href="https://www.linkedin.com/company/acme-labs/">Acme Labs</a><a href="https://www.linkedin.com/company/acme/">Acme</a>'); }
    if (url.pathname.startsWith('/company/')) { hits.companyPaths.push(url.pathname); return html('<a href="/search/results/people/?currentCompany=%5B%22777%22%5D&origin=COMPANY_PAGE_CANNED_SEARCH">10K+ employees</a><a href="http://www.linkedin.com/search/results/people/?origin=COMPANY_PAGE_CANNED_SEARCH&network=%5B%22F%22%5D&currentCompany=%5B%22999%22%5D">A and 2 other connections work here</a>'); }
    if (url.pathname.startsWith('/search/results/people')) { hits.people.push(url.search); return html(searchResultsHtml); }
    return html('');
  };
}

test('people search finds the company by its exact name, filters by its internal number, keeps only current employees it can read, and remembers the number', async () => {
  const root = await mkdtemp(join(tmpdir(), 'job-link-people-search-'));
  try {
    const page = await browser.newPage();
    const hits = newHits();
    await page.route('**/*', linkedinFake(hits));
    const store = new Map<string, string>();
    const cache = { get: (key: string) => store.get(key), set: (key: string, value: string) => { store.set(key, value); } };
    const result = await searchCompanyPeople(page, root, 'Acme', ['QA Manager', 'CEO'], 10, undefined, { cache });
    assert.equal(result.contacts.length, 3); // Jane once (deduped across queries), Bob, and the engineer; the pending one and the unreadable card are left out
    assert.ok(result.contacts.every((contact) => contact.company === 'Acme'));
    const jane = result.contacts.find((contact) => contact.name === 'Jane Doe')!;
    assert.equal(jane.title, 'Chief Executive Officer at Acme'); assert.equal(jane.profileUrl, 'https://www.linkedin.com/in/ceo-jane');
    assert.ok(!result.contacts.some((contact) => /No Headline|Pending/.test(contact.name)));
    assert.ok(result.warnings.some((warning) => /unparseable/.test(warning)));
    assert.deepEqual(hits.companyPaths, ['/company/acme/'], 'Acme, not the similarly named Acme Labs');
    assert.equal(hits.people.length, 2);
    assert.ok(hits.people.every((search) => search.includes('currentCompany=%5B%22777%22%5D')), 'the company number from its own employees link, not the "connections work here" one');
    assert.equal(store.get('company_id:acme'), '777');
    await searchCompanyPeople(page, root, 'Acme', ['CEO'], 10, undefined, { cache });
    assert.equal(hits.companySearch, 1); assert.equal(hits.companyPaths.length, 1, 'the remembered number saves the two lookups');
    await page.close();
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('a very short company name is not guessed on LinkedIn, but an alias for it is used directly', async () => {
  const root = await mkdtemp(join(tmpdir(), 'job-link-people-alias-'));
  try {
    const page = await browser.newPage();
    const hits = newHits();
    await page.route('**/*', linkedinFake(hits));
    const guessed = await searchCompanyPeople(page, root, 'ICE', ['CEO'], 5);
    assert.equal(guessed.contacts.length, 0); assert.match(guessed.warnings.join(' '), /too ambiguous/);
    assert.equal(hits.companySearch + hits.companyPaths.length + hits.people.length, 0, 'no LinkedIn page is opened for an ambiguous name');
    const aliased = await searchCompanyPeople(page, root, 'ICE', ['CEO'], 5, undefined, { aliases: { ICE: 'intercontinental-exchange' } });
    assert.ok(aliased.contacts.length > 0); assert.deepEqual(hits.companyPaths, ['/company/intercontinental-exchange/']); assert.equal(hits.companySearch, 0);
    await page.close();
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('a search-result card is read from its visible lines: name, degree, headline, action and current role', () => {
  const card = parsePersonCard({ href: 'https://www.linkedin.com/in/naveenpalavalasa?miniProfileUrn=x', lines: ['Naveen P', '• 2nd', 'Manager, Talent Acquisition at Infor', 'Hyderabad, Telangana, India', 'Connect', 'Current: Talent Acquisition Manager at Infor', 'Kiran Kumar, Rajesh Y & 29 other mutual connections'] })!;
  assert.deepEqual([card.name, card.degree, card.headline, card.action, card.current], ['Naveen P', '2nd', 'Manager, Talent Acquisition at Infor', 'Connect', 'Talent Acquisition Manager at Infor']);
  assert.equal(card.profileUrl, 'https://www.linkedin.com/in/naveenpalavalasa');
  assert.equal(parsePersonCard({ href: 'https://www.linkedin.com/in/x', lines: ['Ravi Pilla', '• 3rd+', 'Recruiter', 'India', 'Follow', 'Past: Recruiter at Zee'] })!.action, 'Follow');
  assert.equal(parsePersonCard({ href: 'https://www.linkedin.com/in/x', lines: ['LinkedIn Member', '• 3rd+', 'Recruiter'] }), undefined);
  assert.equal(parsePersonCard({ href: 'https://www.linkedin.com/in/x', lines: ['Only A Name'] }), undefined);
  assert.equal(parsePersonCard({ href: 'https://example.com/not-a-profile', lines: ['Name', 'Headline'] }), undefined);
});

test('a LinkedIn login wall stops the search immediately and is reported, not swallowed', async () => {
  const root = await mkdtemp(join(tmpdir(), 'job-link-people-search-blocked-'));
  try {
    const page = await browser.newPage();
    await page.route('**/*', (route) => route.fulfill({ contentType: 'text/html', body: '<html><body><label>Password<input type="password"></label></body></html>' }));
    const result = await searchCompanyPeople(page, root, 'Acme', ['QA Manager', 'CEO', 'HR Manager']);
    assert.equal(result.contacts.length, 0);
    assert.equal(result.blocked?.status, 'LOGIN_REQUIRED');
    await page.close();
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('csv export escapes commas and quotes and includes the related live job', async () => {
  const root = await mkdtemp(join(tmpdir(), 'job-link-csv-'));
  const db = openDatabase(root);
  try {
    db.prepare("INSERT INTO jobs(company,title,location,job_url,source,detected_at,description,status,evidence_mode,updated_at) VALUES('Acme, Inc','QA Engineer, Senior','Remote','https://example.com/job','fixture',?,'QA','APPLICATION_READY','live',?)").run(new Date().toISOString(), new Date().toISOString());
    const path = await exportContactsCsv(root, db, [{ company: 'Acme, Inc', name: 'Jane "JD" Doe', title: 'CEO', profileUrl: 'https://linkedin.com/in/jane', relevanceScore: 95 }]);
    const csv = await readFile(path, 'utf8');
    assert.ok(csv.includes('"Acme, Inc"'));
    assert.ok(csv.includes('"Jane ""JD"" Doe"'));
    assert.ok(csv.includes('"QA Engineer, Senior"'));
    assert.ok(csv.includes('https://example.com/job'));
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});

test('discoverAndExportContacts only searches companies with real live jobs, filters low-relevance results, and merges without discarding manual contacts', async () => {
  const root = await mkdtemp(join(tmpdir(), 'job-link-discover-contacts-'));
  const db = openDatabase(root);
  try {
    await mkdir(join(root, 'config'));
    await writeFile(join(root, 'config', 'contacts.json'), JSON.stringify({ contacts: [{ company: 'Manual Co', name: 'Manually Added', title: 'Recruiter', profileUrl: 'https://linkedin.com/in/manual', relevanceScore: 90 }], sources: [] }));
    db.prepare("INSERT INTO jobs(company,title,location,job_url,source,detected_at,description,status,evidence_mode,updated_at) VALUES('Acme','QA Engineer','Remote','https://example.com/job','fixture',?,'QA','APPLICATION_READY','live',?)").run(new Date().toISOString(), new Date().toISOString());
    db.prepare("INSERT INTO jobs(company,title,location,job_url,source,detected_at,description,status,evidence_mode,updated_at) VALUES('Stale Fixture Co','QA Engineer','Remote','https://example.com/job2','fixture',?,'QA','APPLICATION_READY','fixture',?)").run(new Date().toISOString(), new Date().toISOString());
    const context = await browser.newContext();
    await context.route('**/*', linkedinFake(newHits()));
    const result = await discoverAndExportContacts(root, db, 'test-run', context, 5, ['QA Manager']);
    assert.equal(result.companies, 1); // the fixture-mode job's company must never be searched as if it were real
    assert.equal(result.found, 3);
    assert.equal((db.prepare("SELECT value FROM linkedin_state WHERE key='company_id:acme'").get() as { value: string }).value, '777');
    assert.equal(result.saved, 2); // CEO and QA Manager clear the relevance bar; the unrelated Software Engineer does not
    const saved = JSON.parse(await readFile(join(root, 'config', 'contacts.json'), 'utf8')) as { contacts: Array<{ name: string }> };
    assert.ok(saved.contacts.some((contact) => contact.name === 'Manually Added'));
    assert.ok(saved.contacts.some((contact) => contact.name === 'Jane Doe'));
    assert.ok(!saved.contacts.some((contact) => contact.name === 'Random Engineer'));
    const csv = await readFile(result.csvPath, 'utf8');
    assert.ok(csv.includes('Manually Added'));
    await context.close();
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});
