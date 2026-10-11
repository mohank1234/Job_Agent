import type { Job } from '../core/types.js';

export function plainText(value: unknown): string {
  let text = typeof value === 'string' ? value : '';
  for (let pass = 0; pass < 2; pass++) text = text.replace(/&lt;/gi, '<').replace(/&gt;/gi, '>').replace(/&quot;/gi, '"').replace(/&#39;|&apos;/gi, "'").replace(/&amp;/gi, '&').replace(/&nbsp;/gi, ' ').replace(/&#(\d+);/g, (_, n: string) => Number(n) <= 0x10ffff ? String.fromCodePoint(Number(n)) : '');
  return text.replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, '').replace(/<\/(?:p|div|li|h[1-6])>|<br\s*\/?>/gi, '\n').replace(/<[^>]*>/g, ' ').replace(/[\t ]+/g, ' ').replace(/\n\s*\n/g, '\n').trim();
}

/** Unknown dates remain unknown. We never substitute discovery/update time. */
export function parsePostedAt(value: unknown, now = new Date()): string {
  if (typeof value === 'number' && Number.isFinite(value)) return new Date(value > 1e12 ? value : value * 1000).toISOString();
  if (typeof value !== 'string' || !value.trim()) return '';
  const clean = value.trim().replace(/^posted\s+/i, '');
  const relative = clean.match(/^(?:about\s+)?(\d+)\+?\s*(minute|hour|day|week|month)s?\s+ago$/i);
  if (relative) {
    const units: Record<string, number> = { minute: 60_000, hour: 3_600_000, day: 86_400_000, week: 604_800_000, month: 2_678_400_000 };
    // Rounded site labels conceal up to one unit: choose the oldest plausible time.
    return new Date(now.getTime() - (Number(relative[1]) + 1) * units[relative[2].toLowerCase()]).toISOString();
  }
  if (/^(today|just now)$/i.test(clean)) {
    const day = new Date(now); if (/today/i.test(clean)) day.setUTCHours(0, 0, 0, 0); return day.toISOString();
  }
  if (/^yesterday$/i.test(clean)) { const day = new Date(now); day.setUTCDate(day.getUTCDate() - 1); day.setUTCHours(0, 0, 0, 0); return day.toISOString(); }
  // Date-only values omit time zone; beginning of UTC day is conservative for most sources.
  if (!/^\d{4}-\d{2}-\d{2}(?:T|$)/.test(clean)) return '';
  const timestamp = Date.parse(clean);
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : '';
}

/** How a LinkedIn-discovered job is applied for ('easy_apply' inside LinkedIn, 'external' on the employer's site), as recorded when its page was read. */
export function applyKindOf(metadata: string | null | undefined): string | undefined {
  try { return (JSON.parse(metadata ?? '{}') as { applyKind?: string }).applyKind; } catch { return undefined; }
}
export function isLinkedInUrl(value: string): boolean { try { return /(^|\.)linkedin\.com$/i.test(new URL(value).hostname); } catch { return false; } }

export function canonicalUrl(value: string): string {
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol)) throw new Error('Job URLs must use HTTP(S)');
  if (isLinkedInUrl(value)) {
    const id = url.pathname.match(/\/jobs\/view\/(?:[^/?#]*-)?(\d+)/)?.[1] ?? url.searchParams.get('currentJobId');
    if (id && /^\d+$/.test(id)) return `https://www.linkedin.com/jobs/view/${id}`;
  }
  url.hash = '';
  for (const key of [...url.searchParams.keys()]) if (/^(utm_|trk$|trackingId$|ref$|source$|gh_src$|lever-source$)/i.test(key)) url.searchParams.delete(key);
  url.hostname = url.hostname.toLowerCase();
  url.pathname = url.pathname.replace(/\/+$/, '') || '/';
  url.searchParams.sort();
  return url.href;
}

export function normalizeJob(job: Job): Job {
  const normalized = { ...job, company: plainText(job.company), title: plainText(job.title), location: plainText(job.location), description: plainText(job.description), jobUrl: canonicalUrl(job.jobUrl), applicationUrl: canonicalUrl(job.applicationUrl || job.jobUrl), postedAt: parsePostedAt(job.postedAt), discoveredAt: job.discoveredAt || new Date().toISOString() };
  if (!normalized.title || !normalized.company || !normalized.source) throw new Error('Job requires title, company, and source');
  return normalized;
}

export function workMode(value: unknown): string {
  const text = typeof value === 'string' ? value : '';
  if (/hybrid/i.test(text)) return 'Hybrid';
  if (/remote/i.test(text)) return 'Remote';
  if (/on.?site|office/i.test(text)) return 'On-site';
  return 'Unknown';
}
