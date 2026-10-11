/** Where a job sits in the work order: how recently it was posted first (so fresh jobs are never starved), then how much the location matters to the candidate. */
export const AGE_BATCHES = ['0-1h', '1-3h', '3-6h', '6-12h', '12-24h', 'older than 24h', 'unknown age'] as const;
export type AgeBatch = (typeof AGE_BATCHES)[number];
const BATCH_LIMITS_HOURS = [1, 3, 6, 12, 24];

export function ageBatchIndex(postedAt: string | null | undefined, now = Date.now()): number {
  const age = (now - Date.parse(postedAt ?? '')) / 3_600_000;
  if (!Number.isFinite(age)) return AGE_BATCHES.length - 1;
  const index = BATCH_LIMITS_HOURS.findIndex((limit) => age <= limit);
  return index === -1 ? BATCH_LIMITS_HOURS.length : Math.max(index, 0);
}
export const ageBatch = (postedAt: string | null | undefined, now = Date.now()): AgeBatch => AGE_BATCHES[ageBatchIndex(postedAt, now)];

// Places people write differently; a priority entry matches any of its spellings.
const ALIASES: Record<string, RegExp> = {
  hyderabad: /hyderabad|secunderabad|hitec city|gachibowli|telangana/i,
  bangalore: /bangalore|bengaluru|bengalooru/i,
  bengaluru: /bangalore|bengaluru|bengalooru/i,
  remote: /\bremote\b|work from home|\bwfh\b|anywhere/i
};
export const DEFAULT_LOCATION_PRIORITY = ['Hyderabad', 'Remote', 'Bangalore'];

/** 1 = first priority location … N = last listed, N+1 = any other location. A job in Hyderabad that is also remote counts as Hyderabad. */
export function locationTier(location: string | null | undefined, workMode: string | null | undefined, priority: string[] = DEFAULT_LOCATION_PRIORITY): number {
  const place = `${location ?? ''} ${workMode ?? ''}`;
  const index = priority.findIndex((entry) => (ALIASES[entry.trim().toLowerCase()] ?? new RegExp(entry.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i')).test(place));
  return index === -1 ? priority.length + 1 : index + 1;
}
export const locationTierLabel = (tier: number, priority: string[] = DEFAULT_LOCATION_PRIORITY): string => tier <= priority.length ? priority[tier - 1] : 'Other';

type Rankable = { posted_at?: string | null; location?: string | null; work_mode?: string | null; fit_score?: number | null };
/** Freshest batch first; inside a batch Hyderabad, then Remote, then Bangalore, then the rest; then best fit, then newest. */
export function compareByPriority(priority: string[] = DEFAULT_LOCATION_PRIORITY, now = Date.now()): (a: Rankable, b: Rankable) => number {
  return (a, b) => ageBatchIndex(a.posted_at, now) - ageBatchIndex(b.posted_at, now)
    || locationTier(a.location, a.work_mode, priority) - locationTier(b.location, b.work_mode, priority)
    || (b.fit_score ?? 0) - (a.fit_score ?? 0)
    || (Date.parse(b.posted_at ?? '') || 0) - (Date.parse(a.posted_at ?? '') || 0);
}
