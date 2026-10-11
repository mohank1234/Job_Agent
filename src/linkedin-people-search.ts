import type { Page } from 'playwright';
import type { Contact } from './core/types.js';
import { detectSafetyStop } from './safety.js';
import { reservePage, type PageGuard } from './linkedin-budget.js';
import { companyKey } from './contact-sources.js';

/** Boolean searches LinkedIn understands. Each is one results page, restricted to people who work at the company today. */
export const defaultRoleQueries = [
  '"hiring manager" OR "QA manager" OR "test manager" OR "head of QA" OR "engineering manager" OR "director of engineering"',
  'recruiter OR "talent acquisition" OR "human resources" OR HR',
  'CEO OR founder OR "co-founder" OR CTO OR "head of engineering" OR "VP engineering"'
];

export type PersonCard = { name: string; degree?: string; headline: string; location?: string; action?: string; current?: string; profileUrl: string };
export type CompanyIdCache = { get(key: string): string | undefined; set(key: string, value: string): void };
export type PeopleSearchOptions = {
  cache?: CompanyIdCache;
  /** company name -> LinkedIn company slug, for names that are ambiguous on LinkedIn (config/linkedin-companies.json) */
  aliases?: Record<string, string>;
};
export type PeopleSearchResult = { contacts: Contact[]; cards: PersonCard[]; warnings: string[]; budgetStopped?: boolean; blocked?: { status: 'BLOCKED_SECURITY' | 'LOGIN_REQUIRED'; reason: string; screenshot?: string } };

const normalizeCompany = (value: string): string => companyKey(value.replace(/\.(?:com|io|ai|co|net|org)\b/gi, ''));
// The degree is shown as "• 2nd"; a few symbol characters of any kind before it are tolerated rather than tied to one bullet character.
const degreePattern = /^[^\w\s]{0,4}\s*(?:1st|2nd|3rd\+?)$/;

/** One search-result card, as its visible text lines: name, connection degree, headline, location, the action button, and "Current: ..." */
export function parsePersonCard(raw: { href: string; lines: string[] }, base = 'https://www.linkedin.com'): PersonCard | undefined {
  const lines = raw.lines.map((line) => line.replace(/\s+/g, ' ').trim()).filter(Boolean);
  let profileUrl: string;
  try { const url = new URL(raw.href, base); url.search = ''; url.hash = ''; profileUrl = url.href; } catch { return undefined; }
  if (!/\/in\/[^/]+/.test(profileUrl) || lines.length < 2) return undefined;
  const degreeAt = lines.findIndex((line, index) => index > 0 && degreePattern.test(line));
  const name = lines[0].replace(/\s*[•·]\s*(?:1st|2nd|3rd\+?)$/, '').trim();
  const headline = lines[degreeAt > 0 ? degreeAt + 1 : 1] ?? '';
  if (!name || !headline || /^linkedin member$/i.test(name)) return undefined;
  return {
    name, headline, profileUrl,
    degree: degreeAt > 0 ? lines[degreeAt].replace(/^[^\w\s]{0,4}\s*/, '') : undefined,
    location: lines[degreeAt > 0 ? degreeAt + 2 : 2],
    action: lines.find((line) => /^(?:Connect|Follow|Message|Pending)$/i.test(line)),
    current: lines.find((line) => /^Current:/i.test(line))?.replace(/^Current:\s*/i, '')
  };
}

type Resolved = { id?: string; matched?: string; warning?: string; budgetStopped?: boolean; blocked?: PeopleSearchResult['blocked'] };

/** LinkedIn filters people by an internal company number, not by name; a name search alone returns anyone who mentions the company. */
async function resolveCompanyId(page: Page, root: string, company: string, guard: PageGuard | undefined, options: PeopleSearchOptions): Promise<Resolved> {
  const cacheKey = `company_id:${normalizeCompany(company)}`;
  const cached = options.cache?.get(cacheKey);
  if (cached) return { id: cached };
  const alias = options.aliases?.[company] ?? Object.entries(options.aliases ?? {}).find(([name]) => normalizeCompany(name) === normalizeCompany(company))?.[1];
  let slug = alias;
  let matched = alias;
  if (!slug) {
    const wanted = normalizeCompany(company);
    // A very short name (ICE, EY, 3M) matches unrelated organisations; those need an explicit alias.
    if (wanted.length <= 3) return { warning: `"${company}" is too ambiguous to match on LinkedIn by name; add its slug to config/linkedin-companies.json` };
    if (!await reservePageAsync(guard, 'company-search', company)) return { budgetStopped: true };
    await page.goto(`https://www.linkedin.com/search/results/companies/?keywords=${encodeURIComponent(company)}`, { waitUntil: 'domcontentloaded' });
    const stop = await detectSafetyStop(page, root, `company-search-${company}`);
    if (stop.stopped) return { blocked: { status: stop.status ?? 'BLOCKED_SECURITY', reason: stop.reason!, screenshot: stop.screenshot } };
    await page.locator('a[href*="/company/"]').first().waitFor({ timeout: 10000 }).catch(() => undefined);
    const found = await page.locator('a[href*="/company/"]').evaluateAll((links) => links.map((link) => ({ href: (link as HTMLAnchorElement).href, text: ((link as HTMLElement).innerText || '').trim().split(/\r?\n/)[0].trim() })).filter((item) => item.text));
    const candidates = found.map((item) => ({ ...item, slug: item.href.match(/\/company\/([^/?#]+)/)?.[1] ?? '', key: normalizeCompany(item.text) })).filter((item) => item.slug);
    const best = candidates.find((item) => item.key === wanted) ?? candidates.find((item) => item.key.startsWith(wanted) || wanted.startsWith(item.key));
    if (!best) return { warning: `No LinkedIn company page matched "${company}"` };
    slug = best.slug; matched = best.text;
  }
  if (!await reservePageAsync(guard, 'company-page', slug)) return { budgetStopped: true };
  await page.goto(`https://www.linkedin.com/company/${slug}/`, { waitUntil: 'domcontentloaded' });
  const stop = await detectSafetyStop(page, root, `company-page-${slug}`);
  if (stop.stopped) return { blocked: { status: stop.status ?? 'BLOCKED_SECURITY', reason: stop.reason!, screenshot: stop.screenshot } };
  await page.locator('a[href*="currentCompany="]').first().waitFor({ state: 'attached', timeout: 10000 }).catch(() => undefined);
  const links = await page.locator('a[href*="currentCompany="]').evaluateAll((items) => items.map((item) => (item as HTMLAnchorElement).href).filter((href) => !/[?&]network=/.test(href)));
  const id = links.map((href) => decodeURIComponent(href).match(/currentCompany=\["(\d+)"\]/)?.[1]).find(Boolean);
  if (!id) return { warning: `The LinkedIn page for "${matched}" has no employee list to search` };
  options.cache?.set(cacheKey, id);
  return { id, matched };
}

async function reservePageAsync(guard: PageGuard | undefined, kind: string, detail: string): Promise<boolean> { return !guard || await reservePage(guard, kind, detail); }

/** Read-only: lists people who work at the company now and could be worth writing to. Never connects, messages, or clicks anything on a profile. */
export async function searchCompanyPeople(page: Page, root: string, company: string, queries: string[] = defaultRoleQueries, maxPerQuery = 4, guard?: PageGuard, options: PeopleSearchOptions = {}): Promise<PeopleSearchResult> {
  const contacts: Contact[] = []; const cards: PersonCard[] = []; const warnings: string[] = []; const seen = new Set<string>();
  const resolved = await resolveCompanyId(page, root, company, guard, options);
  if (resolved.blocked) return { contacts, cards, warnings, blocked: resolved.blocked };
  if (resolved.budgetStopped) return { contacts, cards, warnings: [...warnings, 'LinkedIn is paused'], budgetStopped: true };
  if (!resolved.id) return { contacts, cards, warnings: [...warnings, resolved.warning ?? `Could not identify ${company} on LinkedIn`] };
  if (resolved.matched) warnings.push(`Matched "${company}" to the LinkedIn page "${resolved.matched}"`);
  for (const query of queries) {
    if (!await reservePageAsync(guard, 'people-search', `${company}: ${query.slice(0, 40)}`)) return { contacts, cards, warnings: [...warnings, 'LinkedIn is paused'], budgetStopped: true };
    const url = new URL('https://www.linkedin.com/search/results/people/');
    url.searchParams.set('currentCompany', JSON.stringify([resolved.id]));
    url.searchParams.set('keywords', query);
    url.searchParams.set('origin', 'FACETED_SEARCH');
    await page.goto(url.href, { waitUntil: 'domcontentloaded' });
    const stop = await detectSafetyStop(page, root, `people-search-${company}`);
    if (stop.stopped) return { contacts, cards, warnings, blocked: { status: stop.status ?? 'BLOCKED_SECURITY', reason: stop.reason!, screenshot: stop.screenshot } };
    await page.locator('div[role="listitem"] a[href*="/in/"]').first().waitFor({ timeout: 10000 }).catch(() => undefined);
    const raw = await page.locator('div[role="listitem"]').evaluateAll((items) => items.map((item) => ({ href: (item.querySelector('a[href*="/in/"]') as HTMLAnchorElement | null)?.href ?? '', lines: ((item as HTMLElement).innerText || '').split('\n') })).filter((item) => item.href));
    let taken = 0;
    for (const entry of raw) {
      if (taken >= maxPerQuery) break;
      const card = parsePersonCard(entry, page.url());
      if (!card) { warnings.push(`Skipped an unparseable result for "${query.slice(0, 30)}" at ${company}`); continue; }
      if (seen.has(card.profileUrl) || /^pending$/i.test(card.action ?? '')) continue;
      seen.add(card.profileUrl); taken++;
      cards.push(card);
      contacts.push({ company, name: card.name, title: card.headline, profileUrl: card.profileUrl, relevanceScore: 0 });
    }
  }
  return { contacts, cards, warnings };
}
