export class SourceAccessError extends Error {
  constructor(message: string, readonly status: 'FAILED' | 'LOGIN_REQUIRED' | 'BLOCKED_SECURITY', readonly url: string) { super(message); }
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

export async function fetchJson(url: string, timeout = 30_000, init: RequestInit = {}): Promise<unknown> {
  // Retry transient failures only: dropped/refused connections and timeouts (backoff), and one retry for a 5xx.
  // Never retry restrictions or authentication challenges.
  for (let attempt = 0; attempt < 3; attempt++) {
    let response: Response;
    try { response = await fetch(url, { ...init, headers: { Accept: 'application/json', ...init.headers }, signal: AbortSignal.timeout(timeout) }); }
    catch (error) { if (attempt < 2) { await sleep(1500 * (attempt + 1)); continue; } throw error; }
    if (response.status === 401) throw new SourceAccessError('Authentication required', 'LOGIN_REQUIRED', url);
    if ([403, 429].includes(response.status)) throw new SourceAccessError(`Access restricted (HTTP ${response.status})`, 'BLOCKED_SECURITY', url);
    if (response.status >= 500 && attempt === 0) continue;
    if (!response.ok) throw new SourceAccessError(`HTTP ${response.status}: ${response.statusText}`, 'FAILED', url);
    try { return await response.json(); } catch { throw new SourceAccessError('Expected JSON from configured job source', 'FAILED', url); }
  }
  throw new Error('Request failed');
}

export function record(value: unknown): Record<string, unknown> { return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
export function string(value: unknown): string { return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : ''; }
export function array(value: unknown): unknown[] { if (!Array.isArray(value)) throw new Error('Source response does not contain the expected job array'); return value; }
