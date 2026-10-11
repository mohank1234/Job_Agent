import type { BrowserContext } from 'playwright';
import type { Config } from '../config.js';
import type { Job } from '../core/types.js';
import type { JobSource } from './source.js';
import type { SourceConfig } from './settings.js';
import type { PageGuard } from '../linkedin-budget.js';
import { BrowserJobSource } from './browser.js';
import { SourceAccessError } from './http.js';
import { matchesSearch } from '../jobs.js';

/** The same posting appears under a different refId/trackingId in every search; only the numeric id identifies it. */
export function linkedinJobKey(url: string): string {
  const id = url.match(/\/jobs\/view\/(?:[^/?#]*-)?(\d+)/)?.[1];
  if (id) return `linkedin:${id}`;
  try { const parsed = new URL(url); return parsed.origin + parsed.pathname; } catch { return url; }
}

export function linkedinSearchUrls(config: Config, maxSearches = 6): string[] {
  const locations = config.search.locations.length ? config.search.locations : config.candidate.preferred_locations;
  const urls: string[] = [];
  // Location-major: if a run's search budget ends early, the priority locations (Hyderabad, Remote, Bangalore) have already been searched.
  for (const location of locations.length ? locations : [config.candidate.location]) for (const role of config.search.role_keywords) {
    const url = new URL('https://www.linkedin.com/jobs/search/');
    url.searchParams.set('keywords', role); url.searchParams.set('location', /^remote$/i.test(location) ? config.candidate.location.split(',').at(-1)!.trim() : location);
    if (/^remote$/i.test(location)) url.searchParams.set('f_WT', '2');
    url.searchParams.set('f_TPR', `r${Math.floor(config.search.max_age_hours * 3600)}`); url.searchParams.set('sortBy', 'DD');
    urls.push(url.href); if (urls.length >= maxSearches) return urls;
  }
  return urls;
}

/** Browser-only access uses the user's existing session and stops at auth/security walls. */
export class LinkedInJobSource implements JobSource {
  readonly name: string;
  readonly warnings: string[] = [];
  readonly collected: Job[] = [];
  truncated = false;
  constructor(private readonly context: BrowserContext, private readonly settings: Extract<SourceConfig, { type: 'linkedin' }>, private readonly root: string, private readonly guard?: PageGuard) { this.name = settings.name; }
  async discover(config: Config): Promise<Job[]> {
    const urls = this.settings.urls.length ? this.settings.urls : linkedinSearchUrls(config, this.settings.max_searches);
    const potentialSearches = config.search.role_keywords.length * Math.max(1, (config.search.locations.length ? config.search.locations : config.candidate.preferred_locations).length);
    if (!this.settings.urls.length && potentialSearches > urls.length) this.warnings.push(`Search limit: ${urls.length} of ${potentialSearches} role/location combinations requested; increase max_searches to cover all`);
    // One posting commonly matches several keyword searches; fetching it once keeps the page count (and LinkedIn footprint) down.
    const seen = new Set<string>();
    const keep = (jobs: Job[]): void => {
      for (const job of jobs) {
        const id = job.jobUrl.match(/(?:\/jobs\/view\/(?:[^/]*-)?)(\d+)(?:[/?]|$)/)?.[1] || job.externalId?.split(':').at(-1);
        this.collected.push({ ...job, source: 'linkedin', externalId: id, metadata: { ...job.metadata, sourceName: this.name } });
      }
    };
    let slowInARow = 0;
    for (const searchUrl of urls) {
      for (let pageIndex = 0; pageIndex < this.settings.max_pages; pageIndex++) {
        if (this.collected.length >= this.settings.max_jobs) { this.truncated = true; return this.collected; }
        const url = new URL(searchUrl); url.searchParams.set('start', String(pageIndex * 25));
        const adapter = new BrowserJobSource(this.context, { name: this.name, type: 'browser', enabled: true, urls: [url.href], max_jobs: this.settings.max_jobs - this.collected.length, max_pages: 1, requires_login: this.settings.requires_login, scrollToLoad: true,
          cardSelector: '.base-search-card, .job-search-card, .job-card-container', titleSelector: '.base-search-card__title, .job-card-list__title, .job-card-container__link', companySelector: '.base-search-card__subtitle, .job-card-container__primary-description, .artdeco-entity-lockup__subtitle', locationSelector: '.job-search-card__location, .job-card-container__metadata-wrapper', linkSelector: 'a.base-card__full-link, a.job-card-list__title, a.job-card-container__link', postedAtSelector: 'time', externalIdAttribute: 'data-entity-urn', emptyResultsSelector: '.jobs-search-no-results-banner, .no-results',
          detail: {
            descriptionSelector: '.show-more-less-html__markup, .jobs-description-content__text, .jobs-description-content__text--stretch, .jobs-description__content, .jobs-box__html-content, #job-details', postedAtSelector: 'time, .posted-time-ago__text',
            // The authenticated page builds class names at deploy time, so the description is located by its stable visible headings
            // and the employer link by its accessible name instead of by CSS class.
            descriptionTextAnchor: { start: 'About the job', end: 'Set alert for similar jobs' },
            applicationLinkSelector: 'a[aria-label*="Apply on company website" i]', easyApplySelector: '[aria-label*="Easy Apply" i]'
          }
        }, this.root, { guard: this.guard, seen, dedupeKey: linkedinJobKey, shouldHydrate: (job) => matchesSearch(job, config).matches });
        try {
          const jobs = await adapter.discover(config);
          this.warnings.push(...adapter.warnings);
          this.truncated ||= adapter.truncated;
          keep(jobs);
          slowInARow = 0;
          if (adapter.budgetStopped) return this.collected;
          // A full page (25 cards once the list has been scrolled) may have a next one and a short page is the last; a page with nothing new means the pages after it are mostly repeats too.
          if (adapter.cardsFound < 20 || !jobs.length) break;
          if (pageIndex + 1 === this.settings.max_pages) this.truncated = true;
        } catch (error) {
          keep(adapter.collected);
          // One search page that is slow to load or drops its connection is not the whole source failing (live 2026-09-29: a single 30 s
          // timeout discarded every LinkedIn search that day). Skip that search and go on; three in a row means LinkedIn itself is
          // unreachable. A login wall or a block (SourceAccessError) still stops the source at once.
          const message = error instanceof Error ? error.message : String(error);
          if (!(error instanceof SourceAccessError) && /Timeout \d+ms exceeded|net::ERR_/i.test(message) && ++slowInARow < 3) {
            this.warnings.push(`Skipped one search page that did not load (${message.split('\n')[0]}): ${url.href}`);
            break;
          }
          throw error;
        }
      }
    }
    return this.collected;
  }
}
