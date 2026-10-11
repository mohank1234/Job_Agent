import { chromium, type BrowserContext } from 'playwright';
import { mkdir } from 'node:fs/promises';

type Cookie = Parameters<BrowserContext['addCookies']>[0][number];
const linkedinCookie = (cookie: Cookie): boolean => /(^|\.)linkedin\.com$/i.test((cookie.domain ?? '').replace(/^\./, '')) && !!cookie.name && cookie.value !== undefined;

/** Unattended environments have no persistent profile, so a previously exported LinkedIn session is loaded from the environment. */
export async function importSessionCookies(context: BrowserContext, encoded = process.env.LINKEDIN_COOKIES_B64): Promise<number> {
  if (!encoded) return 0;
  try {
    const parsed = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8')) as Cookie[];
    const cookies = (Array.isArray(parsed) ? parsed : []).filter(linkedinCookie);
    if (cookies.length) await context.addCookies(cookies);
    return cookies.length;
  } catch (error) {
    console.warn(`[SESSION] LinkedIn session cookies could not be loaded: ${error instanceof Error ? error.message : String(error)}`);
    return 0;
  }
}

export async function exportSessionCookies(context: BrowserContext): Promise<{ encoded: string; count: number; hasSession: boolean; sessionExpires?: string }> {
  const cookies = (await context.cookies('https://www.linkedin.com')).filter(linkedinCookie);
  const session = cookies.find((cookie) => cookie.name === 'li_at');
  return { encoded: Buffer.from(JSON.stringify(cookies), 'utf8').toString('base64'), count: cookies.length, hasSession: !!session, sessionExpires: session && session.expires > 0 ? new Date(session.expires * 1000).toISOString() : undefined };
}

export async function openPersistentSession(root: string, headless = false): Promise<BrowserContext> {
  await mkdir(`${root}/browser-profile`, { recursive: true });
  const recordVideo=process.env.JOB_LINK_RECORD_VIDEO==='1';
  if(recordVideo)await mkdir(`${root}/reports/videos`,{recursive:true});
  try {
    const context = await chromium.launchPersistentContext(`${root}/browser-profile`, { headless, viewport: { width: 1440, height: 1000 }, acceptDownloads: true, ...(recordVideo ? {recordVideo:{dir:`${root}/reports/videos`}} : {}) });
    context.setDefaultTimeout(12000);
    context.setDefaultNavigationTimeout(45000);
    const loaded = await importSessionCookies(context);
    if (loaded) console.log(`[SESSION] loaded ${loaded} LinkedIn cookies from the environment`);
    return context;
  } catch (error) {
    throw new Error(`Could not open the persistent browser profile. Close other Job Link browser sessions; install Chromium with npx playwright install chromium if missing. ${error instanceof Error ? error.message : String(error)}`);
  }
}

export async function closeSession(context: BrowserContext, timeoutMs = 15_000): Promise<void> {
  // A crashed browser can leave close() waiting forever, which would hold the run lock (and a CI job) until something kills it.
  // The timer must keep the event loop alive until it fires (or is cleared): with a dead browser nothing else does.
  let timer: NodeJS.Timeout | undefined;
  try { await Promise.race([context.close().catch(() => undefined), new Promise<void>((resolve) => { timer = setTimeout(resolve, timeoutMs); })]); }
  finally { if (timer) clearTimeout(timer); }
}
