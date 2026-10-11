import { mkdir, readFile, writeFile } from 'node:fs/promises';
import type { BrowserContext } from 'playwright';
import type Database from 'better-sqlite3';
import type { Contact } from './core/types.js';
import { rankContacts } from './outreach-framework.js';
import { canonicalContactUrl } from './contact-sources.js';
import { defaultRoleQueries, searchCompanyPeople, type CompanyIdCache } from './linkedin-people-search.js';
import { logEvent } from './logger.js';
import type { LinkedInBudget } from './linkedin-budget.js';

export type ContactDiscoveryRunResult = { companies: number; found: number; saved: number; csvPath: string; blocked?: string };

function csvField(value: string): string { return /[",\r\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value; }

export async function exportContactsCsv(root: string, db: Database.Database, contacts: Contact[]): Promise<string> {
  const rows = contacts.map((contact) => {
    const job = db.prepare(`SELECT title, job_url FROM jobs WHERE company = ? AND evidence_mode = 'live' ORDER BY updated_at DESC LIMIT 1`).get(contact.company) as { title: string; job_url: string } | undefined;
    return [contact.company, contact.name, contact.title, contact.profileUrl, String(contact.relevanceScore), job?.title ?? '', job?.job_url ?? '', '', ''];
  });
  const header = ['Company', 'Contact Name', 'Contact Title', 'LinkedIn Profile', 'Relevance Score', 'Related Job Title', 'Related Job URL', 'Connected? (fill in)', 'Outreach Notes (fill in)'];
  const csv = [header, ...rows].map((row) => row.map(csvField).join(',')).join('\r\n') + '\r\n';
  await mkdir(`${root}/reports`, { recursive: true });
  const path = `${root}/reports/contacts-export.csv`;
  await writeFile(path, csv);
  return path;
}

/** Read-only LinkedIn people search for companies with real, already-discovered jobs; writes contacts.json and a CSV for manual outreach. */
export async function discoverAndExportContacts(root: string, db: Database.Database, runId: string, context: BrowserContext, companyLimit = 5, queries: string[] = defaultRoleQueries, budget?: LinkedInBudget, onlyCompanies?: string[]): Promise<ContactDiscoveryRunResult> {
  if (budget && budget.status() !== 'OK') throw new Error(budget.status() === 'BLOCKED' ? budget.describe() : 'LinkedIn daily page budget is already spent; try again tomorrow (UTC).');
  const companies = onlyCompanies?.length ? onlyCompanies : (db.prepare(`SELECT DISTINCT company FROM jobs WHERE evidence_mode = 'live' AND status IN ('REVIEW','APPLICATION_READY','NEEDS_USER_INPUT','SUBMITTED') ORDER BY updated_at DESC LIMIT ?`).all(companyLimit) as Array<{ company: string }>).map((row) => row.company);
  if (!companies.length) throw new Error('No real eligible jobs found yet; run `npm run dev -- run` or `discover` first.');
  const aliases = JSON.parse(await readFile(`${root}/config/linkedin-companies.json`, 'utf8').catch(() => '{}')) as Record<string, string>;
  const cache: CompanyIdCache = {
    get: (key) => (db.prepare('SELECT value FROM linkedin_state WHERE key=?').get(key) as { value: string } | undefined)?.value,
    set: (key, value) => { db.prepare('INSERT INTO linkedin_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key, value); }
  };
  const page = await context.newPage();
  const found: Contact[] = [];
  let blocked: string | undefined;
  try {
    for (const company of companies) {
      const result = await searchCompanyPeople(page, root, company, queries, 4, budget, { cache, aliases });
      found.push(...result.contacts);
      await logEvent(root, { runId, stage: 'PEOPLE_SEARCH', company, found: result.contacts.length, warnings: result.warnings });
      if (result.blocked) { blocked = result.blocked.reason; budget?.recordBlock(`people search: ${result.blocked.reason}`); break; }
      if (result.budgetStopped) { blocked = 'LinkedIn daily page budget reached'; break; }
    }
  } finally { await page.close().catch(() => undefined); }
  const ranked = rankContacts(found).filter((contact) => contact.relevanceScore >= 65);
  const path = `${root}/config/contacts.json`;
  const existing = JSON.parse(await readFile(path, 'utf8').catch(() => '{"contacts":[],"sources":[]}')) as { contacts: Contact[]; sources: unknown[] };
  const byUrl = new Map<string, Contact>();
  for (const contact of existing.contacts ?? []) byUrl.set(canonicalContactUrl(contact.profileUrl), contact);
  for (const contact of ranked) { const profileUrl = canonicalContactUrl(contact.profileUrl); byUrl.set(profileUrl, { ...contact, profileUrl }); }
  existing.contacts = [...byUrl.values()];
  await writeFile(path, `${JSON.stringify(existing, null, 2)}\n`);
  const csvPath = await exportContactsCsv(root, db, existing.contacts);
  return { companies: companies.length, found: found.length, saved: ranked.length, csvPath, blocked };
}
