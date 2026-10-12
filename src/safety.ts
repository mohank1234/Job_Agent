import type { Frame, Page } from 'playwright';
import { mkdir } from 'node:fs/promises';

const stopPatterns = [
  /(?:complete|solve|enter|verify).{0,35}captcha/i, /security verification/i, /identity verification/i,
  /(?:your |this )?account (?:has been |is )?restrict(?:ed|ion)/i, /temporary restriction/i,
  /unusual (?:activity|traffic)/i, /too many requests/i, /suspicious (?:login|activity)/i,
  /(?:detected|suspected) automated activity/i, /verify (?:that )?you(?:'re| are) (?:a )?human/i,
  /(?:complete|requires?|enter|enable|verify).{0,30}(?:multi-factor authentication|two[- ](?:factor|step) (?:authentication|verification))/i,
  /(?:enter|send|sent).{0,40}(?:one[- ]time (?:password|code)|verification code|\botp\b)/i,
  /(?:access (?:has been )?denied|temporarily blocked|checking your browser)/i
];

export function detectSecurityText(text: string): string | undefined {
  return stopPatterns.find((pattern) => pattern.test(text))?.source;
}

/**
 * Which text decides whether a page is a security stop. On LinkedIn a company page, profile or job post can legitimately SAY "identity
 * verification" or "unusual activity" in its own content (a fraud-prevention company's page did, and stopped every people search), so
 * there only dialogs, alerts, headings, or a short stand-alone page count; a checkpoint route always counts in full. Elsewhere the whole page counts.
 */
export function securityScope(url: string, bodyText: string, landmarkText: string): string {
  let host = ''; let path = '';
  try { const parsed = new URL(url); host = parsed.hostname; path = parsed.pathname; } catch { return bodyText; }
  if (!/(^|\.)linkedin\.com$/i.test(host)) return bodyText;
  if (/^\/(?:checkpoint|uas|authwall)(?:\/|$)/i.test(path)) return bodyText;
  return bodyText.replace(/\s+/g, ' ').length < 2000 ? bodyText : landmarkText;
}

type FrameStop = { reason: string; status: 'BLOCKED_SECURITY' | 'LOGIN_REQUIRED' };

async function inspectFrame(frame: Frame): Promise<FrameStop | undefined> {
  const body = await frame.locator('body').innerText().catch(() => '');
  const landmarks = await frame.locator('[role="dialog"], [role="alertdialog"], [role="alert"], h1, h2, h3').allInnerTexts().then((parts) => parts.join(' | ')).catch(() => '');
  const text = detectSecurityText(securityScope(frame.url(), body, landmarks));
  if (text) return { reason: text, status: 'BLOCKED_SECURITY' };
  const challenge = frame.locator('iframe[src*="recaptcha"][title*="challenge" i], iframe[src*="recaptcha"][title="reCAPTCHA"]:not([src*="size=invisible"]), iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], [data-sitekey]:not(.grecaptcha-badge):not([data-size="invisible"])');
  for (let index = 0; index < await challenge.count(); index++) {
    const control = challenge.nth(index);
    const passiveBadge = await control.evaluate((element) => Boolean(element.closest('.grecaptcha-badge'))).catch(() => false);
    if (!passiveBadge && await control.isVisible().catch(() => false)) return { reason: 'Visible CAPTCHA or bot-verification control', status: 'BLOCKED_SECURITY' };
  }
  const password = frame.locator('input[type="password"]');
  const loginRoute = /\/(?:login|signin|sign-in|authwall|checkpoint)(?:[/?#]|$)/i.test(frame.url());
  if (loginRoute || await password.first().isVisible().catch(() => false)) return { reason: 'Login or account authentication required', status: 'LOGIN_REQUIRED' };
  return undefined;
}

// A child frame whose navigation never commits (a stalled ad or tracking iframe) never gets a JavaScript context, and locator.count()
// then waits for one forever, with no timeout of its own. Seen live 2026-09-29: a LinkedIn company page held `find-contacts` for 25
// minutes. Each frame's check is therefore bounded. A CAPTCHA frame is still caught from its parent's DOM (the iframe[src*=...] match
// above), and a frame too stalled to answer has nothing rendered to show; the page itself gets longer before it is given up on.
async function bounded(frame: Frame, ms: number): Promise<FrameStop | undefined> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<undefined>((resolve) => { timer = setTimeout(() => resolve(undefined), ms); });
  try { return await Promise.race([inspectFrame(frame).catch(() => undefined), timeout]); }
  finally { if (timer) clearTimeout(timer); }
}

export async function detectSafetyStop(page: Page, root: string, label: string, frameTimeoutMs = 5000): Promise<{ stopped: boolean; reason?: string; screenshot?: string; status?: 'BLOCKED_SECURITY' | 'LOGIN_REQUIRED'; url?: string }> {
  let reason: string | undefined;
  let status: 'BLOCKED_SECURITY' | 'LOGIN_REQUIRED' = 'BLOCKED_SECURITY';
  for (const frame of page.frames()) {
    const stop = await bounded(frame, frame === page.mainFrame() ? frameTimeoutMs * 4 : frameTimeoutMs);
    if (stop) { reason = stop.reason; status = stop.status; break; }
  }
  if (!reason) return { stopped: false };
  await mkdir(`${root}/screenshots`, { recursive: true });
  const screenshot = `${root}/screenshots/${Date.now()}_${label.replace(/[^a-zA-Z0-9_-]/g, '_')}_safety-stop.png`;
  await page.screenshot({ path: screenshot, fullPage: true, timeout: 15000 }).catch(() => undefined);
  return { stopped: true, reason, screenshot, status, url: page.url() };
}
