import { mkdir } from 'node:fs/promises';
import type { Page } from 'playwright';
import type { OutreachStatus } from './core/types.js';
import { detectSafetyStop } from './safety.js';

function normalized(value: string): string { return value.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim(); }
function meaningfulTokens(value: string): string[] { return normalized(value).split(' ').filter((word) => word.length > 1); }

/** A mistyped or redirected profile URL must never receive a connection note meant for someone else. */
export async function verifyProfileIdentity(page: Page, expectedName: string): Promise<boolean> {
  const heading = (await page.locator('h1').first().textContent({ timeout: 5000 }).catch(() => '')) || '';
  const tokens = meaningfulTokens(expectedName);
  const headingNormalized = normalized(heading);
  return tokens.length > 0 && tokens.every((token) => headingNormalized.includes(token));
}

export type ConnectionState = 'CONNECT_AVAILABLE' | 'ALREADY_CONNECTED' | 'PENDING' | 'FOLLOW_ONLY' | 'UNKNOWN';

export async function detectConnectionState(page: Page): Promise<ConnectionState> {
  if (await page.getByRole('button', { name: /^pending$/i }).first().isVisible().catch(() => false)) return 'PENDING';
  const connect = page.getByRole('button', { name: /^connect$/i }).first();
  if (await connect.isVisible().catch(() => false)) return 'CONNECT_AVAILABLE';
  // Connect is hidden behind an overflow menu on some profile layouts; look without committing to a click yet.
  const more = page.getByRole('button', { name: /^more actions$|^more$/i }).first();
  if (await more.isVisible().catch(() => false)) {
    await more.click({ timeout: 5000 }).catch(() => undefined);
    const menuConnect = page.getByRole('menuitem', { name: /^connect$/i }).first();
    const found = await menuConnect.isVisible({ timeout: 3000 }).catch(() => false);
    await page.keyboard.press('Escape').catch(() => undefined);
    if (found) return 'CONNECT_AVAILABLE';
  }
  if (await page.getByRole('button', { name: /^message$/i }).first().isVisible().catch(() => false)) return 'ALREADY_CONNECTED';
  if (await page.getByRole('button', { name: /^follow$/i }).first().isVisible().catch(() => false)) return 'FOLLOW_ONLY';
  return 'UNKNOWN';
}

export type ConnectAttempt = { opened: boolean; noteFilled: boolean; requiresEmail: boolean };

/** With the invitation dialog already open: enters the note and stops. */
export async function fillInvitationNote(page: Page, message: string): Promise<ConnectAttempt> {
  const dialog = page.getByRole('dialog').first();
  await dialog.waitFor({ state: 'visible', timeout: 8000 });
  if (await dialog.getByText(/share (?:their|the) email address|add their email|enter their email/i).first().isVisible().catch(() => false)) return { opened: true, noteFilled: false, requiresEmail: true };
  const addNote = dialog.getByRole('button', { name: /add a note/i }).first();
  if (await addNote.isVisible().catch(() => false)) await addNote.click({ timeout: 5000 });
  const textbox = dialog.getByRole('textbox').first();
  await textbox.waitFor({ state: 'visible', timeout: 5000 });
  await textbox.fill(message, { timeout: 5000 });
  const value = await textbox.inputValue().catch(async () => (await textbox.textContent()) ?? '');
  return { opened: true, noteFilled: value.trim() === message.trim(), requiresEmail: false };
}

/** Fills the invitation note and stops. Clicking the final "Send invitation" control happens separately, only after the person approves it. */
export async function openConnectDialogAndFillNote(page: Page, message: string): Promise<ConnectAttempt> {
  const directConnect = page.getByRole('button', { name: /^connect$/i }).first();
  if (await directConnect.isVisible().catch(() => false)) await directConnect.click({ timeout: 5000 });
  else {
    const more = page.getByRole('button', { name: /^more actions$|^more$/i }).first();
    await more.click({ timeout: 5000 });
    await page.getByRole('menuitem', { name: /^connect$/i }).first().click({ timeout: 5000 });
  }
  return fillInvitationNote(page, message);
}

/** LinkedIn opens its "Add a note to your invitation" dialog from this address; it needs no visit to the person's profile. */
export function inviteUrlFor(profileUrl: string): string | undefined {
  const vanity = /^https?:\/\/(?:[a-z]+\.)?linkedin\.com\/in\/([^/?#]+)/i.exec(profileUrl)?.[1];
  return vanity ? `https://www.linkedin.com/preload/search-custom-invite/?vanityName=${vanity}` : undefined;
}

/** dialogGone: the invitation dialog was no longer open when Send was needed (closed while waiting for approval, or sent by hand). */
export type SendOutcome = { sent: boolean; reason?: string; limitReached?: boolean; dialogGone?: boolean };

/** Clicks "Send invitation" in the open dialog. Only ever called after the person has approved this exact note. */
export async function sendOpenInvitation(page: Page): Promise<SendOutcome> {
  const dialog = page.getByRole('dialog').first();
  const send = dialog.getByRole('button', { name: /^send( invitation)?$/i }).first();
  if (!await send.isVisible().catch(() => false)) {
    const open = await dialog.isVisible().catch(() => false);
    const shown = open ? ((await dialog.innerText().catch(() => '')) || '').replace(/\s+/g, ' ').trim().slice(0, 160) : '';
    return { sent: false, reason: open ? `The Send button is not visible in the invitation dialog (the dialog says: "${shown}")` : 'The invitation dialog was no longer open', dialogGone: !open };
  }
  await page.waitForTimeout(500); // the button's enabled state and colour settle just after the text is entered
  if (!await send.isEnabled().catch(() => false)) return { sent: false, reason: 'The Send button is disabled (the note may be over 300 characters)' };
  await send.click({ timeout: 5000 });
  const closed = await dialog.waitFor({ state: 'hidden', timeout: 10000 }).then(() => true, () => false);
  if (await page.getByText(/invitation limit|weekly (?:invitation )?limit|reached the (?:weekly )?limit/i).first().isVisible().catch(() => false)) return { sent: false, limitReached: true, reason: 'LinkedIn reports the weekly invitation limit is reached' };
  return closed ? { sent: true } : { sent: false, reason: 'The invitation dialog did not close after Send, so it may not have been sent' };
}

async function captureEvidence(page: Page, root: string, label: string): Promise<string | undefined> {
  await mkdir(`${root}/screenshots`, { recursive: true });
  const path = `${root}/screenshots/${Date.now()}_${label.replace(/[^a-zA-Z0-9_-]/g, '_')}.png`;
  try { await page.screenshot({ path, timeout: 15000 }); return path; } catch { return undefined; }
}

export type ConnectResult = { status: OutreachStatus; reason?: string; screenshot?: string; finalUrl: string };

/** How long to wait for LinkedIn's invitation dialog before concluding that this person cannot be invited that way. */
export const connectTimings = { inviteDialogMs: 8000 };

/** Real LinkedIn: open the invitation dialog directly, check it is for the right person, and fill the note. The dialog stays open for the caller.
 * Returns undefined when no dialog appears (already connected, invitation pending, Connect not offered): the profile page then says which. */
async function prepareInvitation(page: Page, root: string, contactName: string, inviteUrl: string, message: string, label: string): Promise<ConnectResult | undefined> {
  await page.goto(inviteUrl, { waitUntil: 'domcontentloaded' });
  const stop = await detectSafetyStop(page, root, label);
  if (stop.stopped) return { status: stop.status === 'LOGIN_REQUIRED' ? 'LOGIN_REQUIRED' : 'BLOCKED_SECURITY', reason: stop.reason, screenshot: stop.screenshot, finalUrl: page.url() };
  const dialog = page.getByRole('dialog').first();
  const opened = await dialog.waitFor({ state: 'visible', timeout: connectTimings.inviteDialogMs }).then(() => true, () => false);
  if (!opened) return undefined;
  const dialogText = normalized((await dialog.innerText().catch(() => '')) || '');
  const tokens = meaningfulTokens(contactName);
  if (tokens.length && !tokens.every((token) => dialogText.includes(token))) return { status: 'NEEDS_USER_INPUT', reason: `The invitation dialog is not for ${contactName}; nothing was written`, screenshot: await captureEvidence(page, root, label), finalUrl: page.url() };
  const attempt = await fillInvitationNote(page, message);
  if (attempt.requiresEmail) return { status: 'NEEDS_USER_INPUT', reason: 'LinkedIn requires an email address to connect with this out-of-network profile', finalUrl: page.url() };
  if (!attempt.noteFilled) return { status: 'FAILED', reason: 'Could not verify the connection note was entered', screenshot: await captureEvidence(page, root, label), finalUrl: page.url() };
  return { status: 'READY_TO_SEND', reason: 'Note filled; awaiting your approval to send', screenshot: await captureEvidence(page, root, label), finalUrl: page.url() };
}

/** One already-open contact profile page. The caller owns the durable outreach record and the real Send click. */
export async function processOutreachContact(page: Page, root: string, contactName: string, profileUrl: string, message: string, label: string): Promise<ConnectResult> {
  const inviteUrl = inviteUrlFor(profileUrl);
  if (inviteUrl) { const viaInvite = await prepareInvitation(page, root, contactName, inviteUrl, message, label); if (viaInvite) return viaInvite; }
  await page.goto(profileUrl, { waitUntil: 'domcontentloaded' });
  const stop = await detectSafetyStop(page, root, label);
  if (stop.stopped) return { status: stop.status === 'LOGIN_REQUIRED' ? 'LOGIN_REQUIRED' : 'BLOCKED_SECURITY', reason: stop.reason, screenshot: stop.screenshot, finalUrl: page.url() };
  if (!await verifyProfileIdentity(page, contactName)) return { status: 'NEEDS_USER_INPUT', reason: 'Profile name does not match the configured contact; verify the profile URL before connecting', finalUrl: page.url() };
  const state = await detectConnectionState(page);
  if (state === 'ALREADY_CONNECTED') return { status: 'ALREADY_CONNECTED', reason: 'Already a connection', finalUrl: page.url() };
  if (state === 'PENDING') return { status: 'PENDING', reason: 'An invitation to this person is already pending', finalUrl: page.url() };
  if (state === 'FOLLOW_ONLY') return { status: 'NEEDS_USER_INPUT', reason: 'This profile only allows Follow, not Connect', finalUrl: page.url() };
  if (state === 'UNKNOWN') return { status: 'NEEDS_USER_INPUT', reason: 'Could not find a Connect control on this profile', screenshot: await captureEvidence(page, root, label), finalUrl: page.url() };
  const attempt = await openConnectDialogAndFillNote(page, message);
  const stopAfter = await detectSafetyStop(page, root, label);
  if (stopAfter.stopped) return { status: stopAfter.status === 'LOGIN_REQUIRED' ? 'LOGIN_REQUIRED' : 'BLOCKED_SECURITY', reason: stopAfter.reason, screenshot: stopAfter.screenshot, finalUrl: page.url() };
  if (attempt.requiresEmail) return { status: 'NEEDS_USER_INPUT', reason: 'LinkedIn requires an email address to connect with this out-of-network profile', finalUrl: page.url() };
  if (!attempt.noteFilled) return { status: 'FAILED', reason: 'Could not verify the connection note was entered', screenshot: await captureEvidence(page, root, label), finalUrl: page.url() };
  return { status: 'READY_TO_SEND', reason: 'Note filled; review and click Send yourself in the open browser', screenshot: await captureEvidence(page, root, label), finalUrl: page.url() };
}
