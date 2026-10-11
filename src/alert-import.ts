import { readFile } from 'node:fs/promises';
import type { BrowserContext } from 'playwright';
import type Database from 'better-sqlite3';
import type { Job } from './core/types.js';
import type { PageGuard } from './linkedin-budget.js';
import { reservePage } from './linkedin-budget.js';
import { checkSourcePage, hydrateJob } from './sources/browser.js';
import { linkedinJobKey } from './sources/linkedin.js';
import { normalizeJob } from './sources/normalize.js';

/** One posting found in a LinkedIn job-alert email (written by `python run.py mailbox-export`). */
export type AlertJob = { url: string; title: string; company: string; location?: string; receivedAt?: string };

const DETAIL = {
  descriptionSelector: '.show-more-less-html__markup, .jobs-description-content__text, .jobs-description-content__text--stretch, .jobs-description__content, .jobs-box__html-content, #job-details',
  postedAtSelector: 'time, .posted-time-ago__text',
  descriptionTextAnchor: { start: 'About the job', end: 'Set alert for similar jobs' },
  applicationLinkSelector: 'a[aria-label*="Apply on company website" i]', easyApplySelector: '[aria-label*="Easy Apply" i]'
};

/** The alert file's postings that are real LinkedIn job links, one per LinkedIn job number, newest first. */
export function parseAlertFile(text: string): AlertJob[] {
  const raw = JSON.parse(text) as unknown;
  const list = Array.isArray(raw) ? raw : (raw as { jobs?: unknown[] }).jobs ?? [];
  const seen = new Set<string>(); const out: AlertJob[] = [];
  for (const item of list as Array<Record<string, unknown>>) {
    const url = String(item.url ?? '');
    const id = url.match(/linkedin\.com\/(?:comm\/)?jobs\/view\/(?:[^/?#]*-)?(\d+)/i)?.[1];
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push({ url: `https://www.linkedin.com/jobs/view/${id}/`, title: String(item.title ?? '').trim(), company: String(item.company ?? '').trim(), location: String(item.location ?? '').trim(), receivedAt: item.receivedAt ? String(item.receivedAt) : undefined });
  }
  return out.sort((a, b) => (Date.parse(b.receivedAt ?? '') || 0) - (Date.parse(a.receivedAt ?? '') || 0));
}

const known = (db: Database.Database, id: string): boolean =>
  !!db.prepare("SELECT 1 FROM jobs WHERE job_url LIKE ? OR canonical_url LIKE ? OR external_id LIKE ?").get(`%/jobs/view/%${id}%`, `%/jobs/view/%${id}%`, `%${id}`);

/**
 * Opens each alerted LinkedIn job once (paced and counted by the LinkedIn page budget, like every other LinkedIn page), reads its description,
 * posting time, apply type and hiring team, and returns jobs ready for scoring. Postings already in the database are skipped.
 */
export async function loadAlertJobs(root: string, db: Database.Database, context: BrowserContext, alerts: AlertJob[], guard: PageGuard, limit = 40): Promise<{ jobs: Job[]; skippedKnown: number; failed: number; stopped: boolean }> {
  const jobs: Job[] = []; let skippedKnown = 0; let failed = 0; let stopped = false;
  for (const alert of alerts) {
    if (jobs.length >= limit) break;
    const id = alert.url.match(/\/jobs\/view\/(\d+)/)![1];
    if (known(db, id)) { skippedKnown++; continue; }
    if (!await reservePage(guard, 'job-detail', alert.url)) { stopped = true; break; }
    const page = await context.newPage();
    try {
      await page.goto(alert.url, { waitUntil: 'domcontentloaded', timeout: 30_000 });
      await checkSourcePage(page, root, 'LinkedIn alert email', false);
      const base: Job = { title: alert.title, company: alert.company, location: alert.location ?? '', workMode: /remote/i.test(`${alert.location} ${alert.title}`) ? 'Remote' : '', jobUrl: alert.url, applicationUrl: alert.url, source: 'linkedin', externalId: id, postedAt: alert.receivedAt ?? '', description: '', metadata: { sourceName: 'LinkedIn alert email', alertReceivedAt: alert.receivedAt } };
      const hydrated = await hydrateJob(page, base, DETAIL);
      // The email's own title and company are kept when the page cannot be read well enough to replace them.
      if (hydrated.title && hydrated.company) jobs.push(normalizeJob({ ...hydrated, externalId: id }));
      else failed++;
    } catch { failed++; }
    finally { await page.close().catch(() => undefined); }
  }
  return { jobs, skippedKnown, failed, stopped };
}

export const readAlertFile = async (file: string): Promise<AlertJob[]> => parseAlertFile(await readFile(file, 'utf8'));
export { linkedinJobKey };
