import { lookup } from 'node:dns/promises';

// DNS/offline failures from Chromium (net::ERR_*) and Node (getaddrinfo / socket codes).
export const OFFLINE_ERROR = /ERR_NAME_NOT_RESOLVED|ERR_INTERNET_DISCONNECTED|ERR_ADDRESS_UNREACHABLE|ERR_NETWORK_CHANGED|ENOTFOUND|EAI_AGAIN|ENETUNREACH|ENETDOWN/i;

// Node's fetch reports every network failure as a bare "fetch failed"; the real reason is in `cause`.
export function describeError(error: unknown): string {
  if (!(error instanceof Error)) return String(error);
  const cause = (error as { cause?: { code?: string; hostname?: string; message?: string } }).cause;
  if (!cause || typeof cause !== 'object') return error.message;
  const detail = cause.code ? [cause.code, cause.hostname].filter(Boolean).join(' ') : cause.message;
  return detail ? `${error.message} (${detail})` : error.message;
}

const PROBE_HOSTS = ['www.linkedin.com', 'github.com', 'www.google.com'];

// A run started without internet imports nothing and replaces the latest digest with a wall of source failures,
// so refuse to start one. Only proven DNS/offline failures on every probe count; anything else lets the run proceed.
export async function assertOnline(resolve: (host: string) => Promise<unknown> = lookup): Promise<void> {
  const results = await Promise.allSettled(PROBE_HOSTS.map(host => resolve(host)));
  if (results.some(result => result.status === 'fulfilled')) return;
  const reasons = results.map(result => describeError((result as PromiseRejectedResult).reason));
  if (!reasons.every(reason => OFFLINE_ERROR.test(reason))) return;
  throw new Error(`No internet connection: DNS lookup failed for ${PROBE_HOSTS.join(', ')} (${reasons[0]}). Nothing was changed; reconnect and rerun.`);
}
