import { isClosedJob } from '../ats/job-state.js';
import type { BrowserContext, Locator, Page } from 'playwright';
import { mkdir } from 'node:fs/promises';
import type { Config } from '../config.js';
import type { Job } from '../core/types.js';
import type { JobSource } from './source.js';
import { browserSourceSchema, type BrowserSourceConfig } from './settings.js';
import { normalizeJob, parsePostedAt, plainText, workMode } from './normalize.js';
import { detectSafetyStop } from '../safety.js';
import { SourceAccessError, record } from './http.js';
import { reservePage, type PageGuard } from '../linkedin-budget.js';
export type { BrowserSourceConfig } from './settings.js';

export type BrowserSourceOptions = {
  /** Reserves every page load before it happens; a refusal stops the source cleanly with whatever was already collected. */
  guard?: PageGuard;
  /** Cards that fail this check are recorded without opening their detail page. */
  shouldHydrate?: (job: Job) => boolean;
  /** Shared across sources so the same posting is never fetched twice under different searches. */
  seen?: Set<string>;
  /** Identifies a posting independent of tracking parameters; defaults to the full URL. */
  dedupeKey?: (url: string) => string;
};

export class BrowserSourceError extends SourceAccessError {
  constructor(message: string, status: 'FAILED' | 'LOGIN_REQUIRED' | 'BLOCKED_SECURITY', url: string, readonly screenshot?: string) { super(message, status, url); }
}

async function captureSourceEvidence(page: Page, root: string, name: string): Promise<string | undefined> {
  await mkdir(`${root}/screenshots`, { recursive: true });
  const path = `${root}/screenshots/${Date.now()}_source_${name.replace(/[^a-z0-9_-]/gi, '_')}.png`;
  try { await page.screenshot({ path, fullPage: true }); return path; } catch { return undefined; }
}

export async function checkSourcePage(page: Page, root: string, name: string, requiresLogin = false): Promise<void> {
  const stopped = await detectSafetyStop(page, root, `source_${name.replace(/[^a-z0-9_-]/gi, '_')}`);
  if (stopped.stopped) throw new BrowserSourceError(stopped.reason || 'Security challenge', stopped.status || 'BLOCKED_SECURITY', page.url(), stopped.screenshot);
  const loginUrl = /\/(?:login|signin|sign-in|authwall)(?:[/?]|$)/i.test(new URL(page.url()).pathname);
  const passwordForm = requiresLogin && await page.locator('input[type="password"]:visible').count() > 0;
  if (loginUrl || passwordForm) throw new BrowserSourceError('Source session requires manual login', 'LOGIN_REQUIRED', page.url(), await captureSourceEvidence(page, root, name));
}

async function readText(parent: Page | Locator, selector?: string): Promise<string> {
  if (!selector) return '';
  const text = (await parent.locator(selector).first().textContent({ timeout: 1500 }).catch(() => '')) || '';
  // A visible label often sits alongside a duplicate screen-reader-only line (e.g. "Title\nTitle with verification");
  // real titles/labels are never intentionally multi-line, so the first non-empty line is the trustworthy one.
  const line = text.split('\n').map((part) => part.trim()).find((part) => part) || '';
  // Some cards concatenate a visible span and an identical accessibility span with no separator at all ("TitleTitle").
  const half = line.length / 2;
  return Number.isInteger(half) && half > 0 && line.slice(0, half) === line.slice(half) ? line.slice(0, half) : line;
}
/** Sites reuse a single "show more/less" component class across many unrelated truncated blocks (bios, sidebars, badges);
 * the real job description is reliably the longest one, so picking `.first()` risks grabbing the wrong element entirely. */
async function readLongestText(parent: Page | Locator, selector?: string): Promise<string> {
  if (!selector) return '';
  const texts = await parent.locator(selector).allTextContents().catch(() => [] as string[]);
  return texts.map((text) => text.trim()).sort((left, right) => right.length - left.length)[0] || '';
}
function longer(...texts: Array<string | undefined>): string {
  return texts.filter((text): text is string => !!text).sort((left, right) => right.length - left.length)[0] || '';
}
const RELATIVE_AGO = /(?:about\s+)?(\d+)\+?\s*(minute|hour|day|week|month)s?\s+ago/i;
/** The first "10 minutes ago"-style phrase in some text, normalised for parsePostedAt, or '' when there is none. */
export function relativeAgo(text: string): string {
  const match = RELATIVE_AGO.exec(text);
  return match ? `${match[1]} ${match[2].toLowerCase()}${Number(match[1]) === 1 ? '' : 's'} ago` : '';
}
async function readDate(parent: Page | Locator, selector?: string): Promise<string> {
  if (!selector) return '';
  const element = parent.locator(selector).first();
  if (!await element.count()) return ''; // a card you have already viewed has no time element; looking for one must not cost several timeouts
  // A site's own "10 minutes ago" is far more precise than the date-only datetime attribute beside it (which reads as midnight).
  const label = (await element.textContent({ timeout: 1500 }).catch(() => '')) || '';
  return relativeAgo(label) || (await element.getAttribute('datetime', { timeout: 1500 }).catch(() => null)) || readText(parent, selector);
}
/** A job page states its age in the top card ("Reposted 1 week ago"), above the description, rather than in a time element. */
async function agoNearTop(page: Page): Promise<string> {
  const text = (await page.locator('body').innerText({ timeout: 5000 }).catch(() => '')) || '';
  return relativeAgo(text.slice(0, Math.max(0, text.indexOf('About the job')) || 4000));
}
/** LinkedIn draws only the first few result cards (7 of 25) until the list is scrolled; scroll in steps until the count stops growing. */
/** How long to wait for LinkedIn to draw more cards after a scroll before deciding the list has ended (tests shorten it). */
export const discoveryTimings = { scrollGrowMs: 3000 };
async function scrollCardsIntoView(page: Page, cardSelector: string, target = 25): Promise<void> {
  let idle = 0;
  for (let step = 0; step < 24 && idle < 2; step++) {
    const before = await page.locator(cardSelector).count();
    if (before >= target) return;
    await page.evaluate((selector) => {
      let node = document.querySelector(selector) as HTMLElement | null;
      while (node && node !== document.body) {
        if (node.scrollHeight > node.clientHeight + 40 && ['auto', 'scroll'].includes(getComputedStyle(node).overflowY)) { node.scrollTop += node.clientHeight * 0.8; return; }
        node = node.parentElement;
      }
      window.scrollBy(0, window.innerHeight * 0.8);
    }, cardSelector);
    // Wait for the list to draw more cards rather than for a fixed time; two scrolls in a row that add nothing mean the end of the list.
    const grew = await page.waitForFunction(({ selector, before }) => document.querySelectorAll(selector).length > before, { selector: cardSelector, before }, { timeout: discoveryTimings.scrollGrowMs }).then(() => true, () => false);
    idle = grew ? 0 : idle + 1;
  }
}

export function jobPostingFromJsonLd(values: unknown[]): Record<string, unknown> | undefined {
  const pending = [...values];
  while (pending.length) {
    const value = pending.shift();
    if (Array.isArray(value)) { pending.push(...value); continue; }
    const data = record(value);
    if (data['@type'] === 'JobPosting' || Array.isArray(data['@type']) && data['@type'].includes('JobPosting')) return data;
    if (Array.isArray(data['@graph'])) pending.push(...data['@graph']);
  }
  return undefined;
}

/** Sites whose description renders behind build-generated, unstable class names have no reliable CSS target;
 * fall back to the visible page text anchored between known, stable heading strings. */
async function extractByTextAnchor(page: Page, anchor: { start: string; end?: string }): Promise<string> {
  try { await page.getByText(anchor.start, { exact: false }).first().waitFor({ state: 'visible', timeout: 10_000 }); }
  catch { return ''; }
  const body = await page.locator('body').innerText().catch(() => '');
  const startIndex = body.indexOf(anchor.start);
  if (startIndex === -1) return '';
  const from = startIndex + anchor.start.length;
  const endIndex = anchor.end ? body.indexOf(anchor.end, from) : -1;
  return (endIndex === -1 ? body.slice(from, from + 8000) : body.slice(from, endIndex)).trim();
}

/** LinkedIn hides the real employer link behind /safety/go/?url=<target> (older pages: /jobs/view/externalApply/<id>?url=<target>). */
export function unwrapRedirect(href: string, base: string): string {
  const url = new URL(href, base);
  if (/(^|\.)linkedin\.com$/i.test(url.hostname) && /^\/(?:safety\/go|jobs\/view\/externalApply\/[^/]+)\/?$/i.test(url.pathname)) {
    const target = url.searchParams.get('url');
    if (target) { try { const parsed = new URL(target); if (/^https?:$/.test(parsed.protocol)) return parsed.href; } catch { /* keep the wrapped URL */ } }
  }
  return url.href;
}

export type HiringTeamMember = { name: string; title: string; profileUrl: string };
/** "Amanpreet Kaur • 2nd Technical HR Recruiter @ Acme | MBA Job poster" -> name and headline (live LinkedIn text, 2026-10-11). */
export function parseHiringTeamEntry(text: string, href: string): HiringTeamMember | undefined {
  const flat = text.replace(/\s+/g, ' ').replace(/\s*Job poster\s*$/i, '').replace(/\s*Message\s*$/i, '').trim();
  const [name, rest = ''] = flat.split(/\s*[•·]\s*/, 2);
  const title = rest.replace(/^(?:1st|2nd|3rd\+?)\s*/i, '').trim();
  let profileUrl = '';
  try { const url = new URL(href); if (/(^|\.)linkedin\.com$/i.test(url.hostname) && /^\/in\/[^/]+/.test(url.pathname)) profileUrl = `https://www.linkedin.com${url.pathname.replace(/\/$/, '')}`; } catch { return undefined; }
  return name.trim() && profileUrl ? { name: name.trim(), title, profileUrl } : undefined;
}
/** The people under "Meet the hiring team" on a LinkedIn job page; the one marked "Job poster" is first. */
export async function readHiringTeam(page: Page): Promise<HiringTeamMember[]> {
  if (!/(^|\.)linkedin\.com$/i.test(new URL(page.url()).hostname)) return [];
  const entries = await page.evaluate(() => {
    const heading = [...document.querySelectorAll('h2,h3,h4,p,span,div')].find((el) => /^meet the hiring team$/i.test(((el as HTMLElement).innerText || '').trim()));
    let box: HTMLElement | null = heading as HTMLElement | null;
    for (let i = 0; i < 4 && box && box.querySelectorAll('a[href*="/in/"]').length === 0; i++) box = box.parentElement;
    return [...(box?.querySelectorAll('a[href*="/in/"]') ?? [])].map((a) => ({ href: (a as HTMLAnchorElement).href, text: ((a as HTMLElement).innerText || '').slice(0, 400) }));
  }).catch(() => [] as Array<{ href: string; text: string }>);
  const team = entries.flatMap((entry) => { const member = parseHiringTeamEntry(entry.text, entry.href); return member ? [{ ...member, poster: /job poster/i.test(entry.text) }] : []; });
  return team.sort((a, b) => Number(b.poster) - Number(a.poster)).map(({ name, title, profileUrl }) => ({ name, title, profileUrl })).filter((member, index, all) => all.findIndex((other) => other.profileUrl === member.profileUrl) === index);
}

export async function hydrateJob(page: Page, job: Job, detail?: BrowserSourceConfig['detail']): Promise<Job> {
  if (isClosedJob(await page.locator('body').innerText())) return {...job,description:'Not currently accepting applications',metadata:{...job.metadata,closed:true}};
  const rawValues = await page.locator('script[type="application/ld+json"]').allTextContents();
  const values = rawValues.flatMap(value => { try { return [JSON.parse(value) as unknown]; } catch { return []; } });
  const structured = jobPostingFromJsonLd(values);
  const cssDescription = detail?.descriptionSelector ? await readLongestText(page, detail.descriptionSelector) : '';
  // Whichever source is longer is almost always the real description; a short match from either source is more likely a mislabeled fragment than a genuinely terse job posting.
  let description = longer(cssDescription, plainText(structured?.description));
  if (description.length < 80 && detail?.descriptionTextAnchor) description = longer(description, await extractByTextAnchor(page, detail.descriptionTextAnchor));
  const date = detail?.postedAtSelector ? await readDate(page, detail.postedAtSelector) : '';
  const applySelectors = [detail?.applicationLinkSelector, detail?.easyApplySelector].filter((selector): selector is string => !!selector);
  // The apply control renders after the description on client-rendered pages; wait for either kind before reading it.
  if (applySelectors.length) await page.locator(applySelectors.join(', ')).first().waitFor({ state: 'attached', timeout: 5000 }).catch(() => undefined);
  const applyLink = detail?.applicationLinkSelector ? await page.locator(detail.applicationLinkSelector).first().getAttribute('href', { timeout: 1500 }).catch(() => null) : null;
  const easyApply = !applyLink && detail?.easyApplySelector ? await page.locator(detail.easyApplySelector).first().isVisible().catch(() => false) : false;
  const applicationUrl = applyLink ? unwrapRedirect(applyLink, page.url()) : job.applicationUrl;
  const external = !!applyLink && !!applicationUrl && !!job.jobUrl && new URL(applicationUrl).hostname !== new URL(job.jobUrl).hostname;
  const pageAgo = date || structured?.datePosted ? '' : await agoNearTop(page);
  const hiringTeam = await readHiringTeam(page);
  return { ...job, description: description || job.description, postedAt: parsePostedAt(date || structured?.datePosted || pageAgo) || job.postedAt, applicationUrl, metadata: { ...job.metadata, detailUrl: page.url(), structuredDatePosted: structured?.datePosted, ...(hiringTeam.length ? { hiringTeam } : {}), ...(external ? { applyKind: 'external' } : easyApply ? { applyKind: 'easy_apply' } : {}) } };
}

export class BrowserJobSource implements JobSource {
  readonly name: string;
  readonly settings: BrowserSourceConfig;
  readonly warnings: string[] = [];
  readonly collected: Job[] = [];
  truncated = false;
  budgetStopped = false;
  cardsProcessed = 0;
  /** The most cards seen on any one results page, whether or not they could all be read: a full page means there may be another. */
  cardsFound = 0;
  constructor(private readonly context: BrowserContext, sourceConfig: BrowserSourceConfig, private readonly root = process.cwd(), private readonly options: BrowserSourceOptions = {}) { this.settings = browserSourceSchema.parse(sourceConfig); this.name = this.settings.name; }
  private async allow(kind: string, detail: string): Promise<boolean> {
    if (!this.options.guard || await reservePage(this.options.guard, kind, detail)) return true;
    this.truncated = true; this.budgetStopped = true;
    this.warnings.push('LinkedIn daily page budget is spent or LinkedIn is paused; remaining pages skipped');
    return false;
  }

  async discover(_config: Config): Promise<Job[]> {
    const settings = this.settings;
    const seen = this.options.seen ?? new Set<string>();
    for (const url of settings.urls) {
      if (!await this.allow('job-search', url)) return this.collected;
      const page = await this.context.newPage();
      try {
        const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 });
        if (response && [401, 403, 429].includes(response.status())) throw new BrowserSourceError(`HTTP ${response.status()}`, response.status() === 401 ? 'LOGIN_REQUIRED' : 'BLOCKED_SECURITY', url, await captureSourceEvidence(page, this.root, this.name));
        for (let pageIndex = 0; pageIndex < settings.max_pages; pageIndex++) {
          await checkSourcePage(page, this.root, this.name, settings.requires_login);
          await page.locator(settings.cardSelector).first().waitFor({ state: 'attached', timeout: 10_000 }).catch(() => undefined);
          await checkSourcePage(page, this.root, this.name, settings.requires_login);
          if (settings.scrollToLoad) await scrollCardsIntoView(page, settings.cardSelector);
          const cards = page.locator(settings.cardSelector);
          const count = await cards.count();
          this.cardsFound = Math.max(this.cardsFound, count);
          if (!count) {
            if (settings.emptyResultsSelector && await page.locator(settings.emptyResultsSelector).count()) break;
            throw new BrowserSourceError('No configured job cards found; source selectors, login, or page layout require attention', 'FAILED', page.url(), await captureSourceEvidence(page, this.root, this.name));
          }
          for (let index = 0; index < count; index++) {
            if (this.collected.length >= settings.max_jobs) { this.truncated = true; return this.collected; }
            const card = cards.nth(index);
            const title = await readText(card, settings.titleSelector);
            const company = settings.company || await readText(card, settings.companySelector);
            const location = await readText(card, settings.locationSelector);
            const description = await readText(card, settings.descriptionSelector);
            const link = await card.locator(settings.linkSelector).first().getAttribute('href', { timeout: 1500 }).catch(() => null);
            if (!title || !company || !link) { this.warnings.push(`Skipped incomplete card ${index + 1} at ${page.url()}`); continue; }
            const jobUrl = new URL(link, page.url()).href;
            this.cardsProcessed++;
            const dedupeKey = this.options.dedupeKey ? this.options.dedupeKey(jobUrl) : jobUrl;
            if (seen.has(dedupeKey)) continue;
            seen.add(dedupeKey);
            const externalId = settings.externalIdAttribute ? await card.getAttribute(settings.externalIdAttribute) : null;
            const applicationLink = settings.applicationLinkSelector ? await card.locator(settings.applicationLinkSelector).first().getAttribute('href', { timeout: 1500 }).catch(() => null) : null;
            // Cards you have already opened show "Viewed" instead of a time element; their text may still say how old they are.
            const posted = parsePostedAt(await readDate(card, settings.postedAtSelector)) || parsePostedAt(relativeAgo((await card.innerText({ timeout: 1500 }).catch(() => '')) || ''));
            let job: Job = { title, company, location, description, workMode: workMode(location), jobUrl, applicationUrl: applicationLink ? new URL(applicationLink, page.url()).href : jobUrl, source: `browser:${this.name}`, externalId: externalId || undefined, postedAt: posted, metadata: { sourceName: this.name, searchUrl: url } };
            if (settings.detail && (!this.options.shouldHydrate || this.options.shouldHydrate(job))) {
              if (!await this.allow('job-detail', jobUrl)) return this.collected;
              const detail = await this.context.newPage();
              try { await detail.goto(jobUrl, { waitUntil: 'domcontentloaded', timeout: 30_000 }); await checkSourcePage(detail, this.root, this.name, settings.requires_login); job = await hydrateJob(detail, job, settings.detail); }
              finally { await detail.close(); }
            }
            this.collected.push(normalizeJob(job));
          }
          if (!settings.nextPageSelector) break;
          const next = page.locator(settings.nextPageSelector).first();
          if (!await next.isVisible() || await next.isDisabled()) break;
          if (pageIndex + 1 >= settings.max_pages) { this.truncated = true; break; }
          if (!await this.allow('job-search', `${url}#next`)) return this.collected;
          const previous = await cards.first().textContent();
          await next.click();
          await page.waitForFunction(({ selector, previous }) => document.querySelector(selector)?.textContent !== previous, { selector: settings.cardSelector, previous }, { timeout: 10_000 }).catch(() => undefined);
        }
      } finally { await page.close(); }
    }
    return this.collected;
  }
}
