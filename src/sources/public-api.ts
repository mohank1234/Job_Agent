import type { Config } from '../config.js';
import type { Job } from '../core/types.js';
import type { JobSource } from './source.js';
import type { SourceConfig } from './settings.js';
import { array, fetchJson, record, string, SourceAccessError } from './http.js';
import { normalizeJob, parsePostedAt, plainText, workMode } from './normalize.js';

type ApiSourceConfig = Extract<SourceConfig, { type: 'greenhouse' | 'lever' | 'ashby' | 'workday' }>;

export class PublicApiJobSource implements JobSource {
  readonly name: string;
  readonly warnings: string[] = [];
  readonly collected: Job[] = [];
  truncated = false;
  constructor(readonly settings: ApiSourceConfig, private readonly timeout = 30_000) { this.name = settings.name; }
  async discover(_config: Config): Promise<Job[]> {
    if (this.settings.type === 'workday') return this.workday();
    const source = this.settings;
    const endpoint = source.type === 'greenhouse' ? `https://boards-api.greenhouse.io/v1/boards/${source.board}/jobs?content=true`
      : source.type === 'lever' ? `https://api.${source.region === 'eu' ? 'eu.' : ''}lever.co/v0/postings/${source.board}?mode=json`
      : `https://api.ashbyhq.com/posting-api/job-board/${encodeURIComponent(source.board)}`;
    const jobs = this.collected;
    let offset = 0;
    do {
      const pageSize = Math.min(100, source.max_jobs - jobs.length);
      const response = await fetchJson(source.type === 'lever' ? `${endpoint}&skip=${offset}&limit=${pageSize}` : endpoint, this.timeout);
      const rows = array(source.type === 'lever' ? response : record(response).jobs);
      for (const raw of rows) {
        const data = record(raw);
        if (source.type === 'ashby' && data.isListed === false) continue;
        if (jobs.length >= source.max_jobs) { this.truncated = true; break; }
        try {
          const categories = record(data.categories);
          const location = source.type === 'greenhouse' ? string(record(data.location).name) : source.type === 'lever' ? string(categories.location) : [string(data.location), ...(Array.isArray(data.secondaryLocations) ? data.secondaryLocations.map(value => string(record(value).location)) : [])].filter(Boolean).join('; ');
          const url = string(data.absolute_url || data.hostedUrl || data.jobUrl);
          const id = string(data.id) || url.split('/').at(-1) || '';
          const description = source.type === 'lever' ? [data.openingPlain, data.descriptionBodyPlain || data.descriptionPlain || data.description, ...(Array.isArray(data.lists) ? data.lists.map(value => `${string(record(value).text)}\n${plainText(record(value).content)}`) : []), data.additionalPlain].map(plainText).filter(Boolean).join('\n') : plainText(data.content || data.descriptionPlain || data.descriptionHtml);
          const date = source.type === 'greenhouse' ? data.first_published : source.type === 'lever' ? data.createdAt : data.publishedAt;
          jobs.push(normalizeJob({ company: string(data.company_name) || source.company, title: string(data.title || data.text), location, workMode: workMode(data.workplaceType || (data.isRemote ? 'remote' : location)), jobUrl: url,
            applicationUrl: source.type === 'greenhouse' ? `https://job-boards.greenhouse.io/${source.board}/jobs/${id}` : string(data.applyUrl) || url,
            source: `${source.type}:${source.board}`, externalId: id, atsJobId: `${source.type}:${source.board}:${id}`, postedAt: parsePostedAt(date), description,
            metadata: { sourceName: source.name, board: source.board, dateField: source.type === 'greenhouse' ? 'first_published' : source.type === 'lever' ? 'createdAt' : 'publishedAt', sourceUpdatedAt: data.updated_at, requisitionId: data.requisition_id, department: data.department || categories.department, address: data.address, sourceMetadata: data.metadata, employmentType: data.employmentType || categories.commitment }
          }));
        } catch (error) { this.warnings.push(`Invalid job ${string(data.id)}: ${String(error)}`); }
      }
      offset += rows.length;
      if (source.type !== 'lever' || rows.length < pageSize) break;
      if (jobs.length >= source.max_jobs) { this.truncated = true; break; }
    } while (true);
    const missingDates = jobs.filter(job => !job.postedAt).length;
    if (missingDates) this.warnings.push(`${missingDates} jobs have no verifiable publication timestamp; strict age filtering will exclude them`);
    return jobs;
  }

  private async workday(): Promise<Job[]> {
    const source = this.settings;
    if (source.type !== 'workday') return [];
    const origin = new URL(source.url).origin;
    const endpoint = `${origin}/wday/cxs/${source.tenant}/${source.site}`;
    const jobs = this.collected;
    for (let page = 0; page < source.max_pages && jobs.length < source.max_jobs; page++) {
      const response = record(await fetchJson(`${endpoint}/jobs`, this.timeout, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ appliedFacets: {}, limit: 20, offset: page * 20, searchText: source.search_text }) }));
      const rows = array(response.jobPostings);
      for (const raw of rows) {
        if (jobs.length >= source.max_jobs) { this.truncated = true; break; }
        const item = record(raw);
        const path = string(item.externalPath);
        if (!path.startsWith('/job/')) { this.warnings.push('Skipped job without a Workday externalPath'); continue; }
        try {
          const detail = record(record(await fetchJson(`${endpoint}${path}`, this.timeout)).jobPostingInfo);
          const id = string(detail.jobReqId) || string(item.bulletFields && Array.isArray(item.bulletFields) ? item.bulletFields[0] : '') || path;
          const url = string(detail.externalUrl) || `${origin}/${source.site}${path}`;
          const location = string(detail.location || item.locationsText);
          jobs.push(normalizeJob({ company: source.company, title: string(detail.title || item.title), location, workMode: workMode(detail.remoteType || location), jobUrl: url, applicationUrl: `${url.replace(/\/apply$/, '')}/apply`, source: `workday:${source.tenant}:${source.site}`, externalId: id, atsJobId: `workday:${source.tenant}:${id}`, description: plainText(detail.jobDescription), postedAt: parsePostedAt(detail.startDate || item.postedOn), metadata: { sourceName: source.name, dateField: detail.startDate ? 'startDate' : 'postedOn (rounded label)', postedOn: item.postedOn, timeType: detail.timeType, sourceUrl: source.url } }));
        } catch (error) {
          if (error instanceof SourceAccessError && error.status !== 'FAILED') throw error;
          this.warnings.push(`Job ${path}: ${String(error)}`);
        }
      }
      if (rows.length < 20 || (page + 1) * 20 >= Number(response.total || Infinity)) return jobs;
      if (page + 1 === source.max_pages || jobs.length >= source.max_jobs) this.truncated = true;
    }
    return jobs;
  }
}
