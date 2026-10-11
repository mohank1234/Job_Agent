import type { BrowserContext } from 'playwright';
import type { Config } from '../config.js';
import type { Job } from '../core/types.js';
import type { SourceConfig, SourceSettings } from './settings.js';
import { BrowserJobSource, BrowserSourceError } from './browser.js';
import { PublicApiJobSource } from './public-api.js';
import { LinkedInJobSource } from './linkedin.js';
import { SourceAccessError } from './http.js';
import type { LinkedInBudget } from '../linkedin-budget.js';
import { describeError } from '../network.js';
export { loadSourceSettings } from './settings.js';

export type SourceResult = { name: string; type: string; status: 'SUCCESS' | 'PARTIAL' | 'FAILED' | 'LOGIN_REQUIRED' | 'BLOCKED_SECURITY'; discovered: number; error?: string; url?: string; screenshot?: string; truncated?: boolean; durationMs: number };
export type ConfiguredDiscovery = { jobs: Job[]; sources: SourceResult[] };
export type SourceConsumer = (jobs: Job[], result: SourceResult) => Promise<void>;

export async function discoverConfiguredSources(root: string, config: Config, settings: SourceSettings, getContext: () => Promise<BrowserContext>, onSource?: SourceConsumer, budget?: LinkedInBudget): Promise<ConfiguredDiscovery> {
  const configured: SourceConfig[] = [...settings.sources, ...settings.browser_sources].filter(source => source.enabled);
  if (!configured.length) throw new Error('No enabled live sources in config/sources.json. Configure at least one real company board or browser source.');
  const results: ConfiguredDiscovery = { jobs: [], sources: [] };
  const runSource = async (source: SourceConfig): Promise<void> => {
    const started = Date.now();
    let adapter: PublicApiJobSource | BrowserJobSource | LinkedInJobSource | undefined;
    let jobs: Job[];
    let result: SourceResult;
    if (source.type === 'linkedin' && budget && budget.status() !== 'OK') {
      const paused = budget.status() === 'BLOCKED';
      const skipped: SourceResult = { name: source.name, type: source.type, status: paused ? 'BLOCKED_SECURITY' : 'PARTIAL', discovered: 0, error: paused ? budget.describe() : 'LinkedIn daily page budget already spent; skipped until tomorrow (UTC)', durationMs: 0 };
      console.log(`[DISCOVERY] ${source.name}: skipped — ${skipped.error}`);
      results.sources.push(skipped);
      await onSource?.([], skipped);
      return;
    }
    console.log(`[DISCOVERY] ${source.name}: querying ${source.type}`);
    try {
      adapter = source.type === 'browser' ? new BrowserJobSource(await getContext(), source, root)
        : source.type === 'linkedin' ? new LinkedInJobSource(await getContext(), source, root, budget) : new PublicApiJobSource(source, settings.request_timeout_ms);
      jobs = await adapter.discover(config);
      const warnings = adapter.warnings;
      // A source that stopped at its own configured limit is PARTIAL too; say so, or the digest shows PARTIAL with a blank reason.
      const limitNote = adapter.truncated ? `Stopped at this source's configured limit (max_jobs ${source.max_jobs}${'max_pages' in source ? `, max_pages ${source.max_pages}` : ''} in config/sources.json) after ${jobs.length} jobs; later results were not read` : undefined;
      result = { name: source.name, type: source.type, status: warnings.length || adapter.truncated ? 'PARTIAL' : 'SUCCESS', discovered: jobs.length, error: [...warnings.slice(0, 20), ...(limitNote ? [limitNote] : [])].join('; ') || undefined, truncated: adapter.truncated, durationMs: Date.now() - started };
      console.log(`[DISCOVERY] ${source.name}: ${jobs.length} jobs${adapter.truncated ? ' (source limit reached)' : ''}${warnings.length ? `; ${warnings.length} warnings: ${warnings[0]}` : ''}`);
    } catch (error) {
      jobs = adapter && 'collected' in adapter ? adapter.collected : [];
      const status = error instanceof SourceAccessError ? error.status : 'FAILED';
      result = { name: source.name, type: source.type, status, discovered: jobs.length, error: describeError(error), url: error instanceof SourceAccessError ? error.url : undefined, screenshot: error instanceof BrowserSourceError ? error.screenshot : undefined, durationMs: Date.now() - started };
      console.log(`[DISCOVERY] ${source.name}: ${status} — ${result.error}`);
    }
    if (source.type === 'linkedin' && budget && (result.status === 'LOGIN_REQUIRED' || result.status === 'BLOCKED_SECURITY')) {
      const until = budget.recordBlock(`${source.name}: ${result.error ?? result.status}`);
      console.log(`[DISCOVERY] ${source.name}: LinkedIn paused until ${until.toISOString()} to avoid repeated blocked attempts`);
    }
    // Persistence errors must propagate, never be misreported as another source failure.
    results.jobs.push(...jobs);
    results.sources.push(result);
    await onSource?.(jobs, result);
  };
  const apis = configured.filter(source => !['browser', 'linkedin'].includes(source.type));
  let next = 0;
  let consumerFailed = false;
  async function worker(): Promise<void> {
    while (next < apis.length && !consumerFailed) {
      try { await runSource(apis[next++]); }
      catch (error) { consumerFailed = true; throw error; }
    }
  }
  // Finish in-flight callbacks before returning a failure so callers can safely close the database.
  const workers = await Promise.allSettled(Array.from({ length: Math.min(settings.concurrency, apis.length) }, () => worker()));
  const failures = workers.filter((worker): worker is PromiseRejectedResult => worker.status === 'rejected');
  if (failures.length) throw new AggregateError(failures.map(failure => failure.reason), 'Failed to persist completed discovery sources');
  for (const source of configured.filter(source => ['browser', 'linkedin'].includes(source.type))) await runSource(source);
  return results;
}
