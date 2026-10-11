import type { BrowserContext, Page } from 'playwright';
import type Database from 'better-sqlite3';
import { processOutreachContact, sendOpenInvitation, type ConnectResult, type SendOutcome } from './linkedin-connect.js';
import { logEvent } from './logger.js';
import { reservePage, type LinkedInBudget } from './linkedin-budget.js';
import { applyLedger, readLedger, recordOutreach, type LedgerStatus } from './outreach-ledger.js';

export type OutreachSendResult = { attempted: number; readyToSend: number; sent: number; alreadyConnected: number; pending: number; needsUserInput: number; blocked: number; failed: number; declined: number };
type OutreachRow = { id: number; job_id: number; message: string; company: string; contact_name: string; contact_title: string; profile_url: string };
export type SendDecision = boolean | 'skip' | 'stop';
export type OutreachSendOptions = {
  /** true: the tool clicks Send once the confirm callback approves. false (default): the person clicks Send in the browser and the callback only confirms they did. */
  clickSend?: boolean;
  /** Fill every note and show it, but never ask to send and never click anything. */
  dryRun?: boolean;
  /** Opens a fresh browser when the current one has closed or crashed, so one crash does not end the batch. */
  reopen?: () => Promise<BrowserContext>;
};

const CLOSED = /Target (?:page, context or browser has been )?closed|Target crashed|browser has been closed|has been closed|browser (?:context )?closed|crashed/i;
const message = (error: unknown): string => (error instanceof Error ? error.message : String(error));

/** Opens each ranked, previously-drafted LinkedIn contact and fills the note. Nothing is sent unless the person approves that exact note. */
export async function sendPreparedOutreach(root: string, db: Database.Database, runId: string, context: BrowserContext, confirmSent: (prompt: string) => Promise<SendDecision>, limit = 50, budget?: LinkedInBudget, options: OutreachSendOptions = {}): Promise<OutreachSendResult> {
  // What really happened on LinkedIn outlives any replacement of the database; re-assert it before choosing who is left to invite.
  const restored = applyLedger(db, await readLedger(root));
  if (restored.updated || restored.inserted) await logEvent(root, { runId, stage: 'OUTREACH_SEND', action: 'ledger-reapplied', ...restored });
  const rows = db.prepare(`SELECT o.id, o.job_id, o.message, c.company, c.name contact_name, c.title contact_title, c.linkedin_url profile_url
    FROM outreach o JOIN contacts c ON c.id = o.contact_id
    WHERE o.status IN ('OUTREACH_READY','FAILED') AND o.type = 'CONNECTION'
    ORDER BY c.relevance_score DESC, o.id LIMIT ?`).all(limit) as OutreachRow[];
  const result: OutreachSendResult = { attempted: 0, readyToSend: 0, sent: 0, alreadyConnected: 0, pending: 0, needsUserInput: 0, blocked: 0, failed: 0, declined: 0 };
  const setStatus = db.prepare('UPDATE outreach SET status=? WHERE id=?');
  let ctx = context;
  const newPage = async (): Promise<Page> => {
    try { return await ctx.newPage(); }
    catch (error) {
      if (!options.reopen || !CLOSED.test(message(error))) throw error;
      await logEvent(root, { runId, stage: 'OUTREACH_SEND', action: 'browser-reopened', reason: message(error) });
      ctx = await options.reopen();
      return await ctx.newPage();
    }
  };
  const attempt = async (row: OutreachRow, label: string): Promise<{ page: Page; outcome: ConnectResult }> => {
    const page = await newPage();
    try { return { page, outcome: await processOutreachContact(page, root, row.contact_name, row.profile_url, row.message, label) }; }
    catch (error) { return { page, outcome: { status: 'FAILED', reason: message(error), finalUrl: page.url() } }; }
  };
  for (const row of rows) {
    if (budget && !await reservePage(budget, 'profile', row.profile_url)) break;
    result.attempted++;
    const remember = (status: LedgerStatus) => recordOutreach(root, { at: new Date().toISOString(), status, url: row.profile_url, name: row.contact_name, title: row.contact_title, company: row.company, message: row.message, jobId: row.job_id });
    let { page, outcome } = await attempt(row, `outreach-${row.id}`);
    if (outcome.status === 'FAILED' && options.reopen && CLOSED.test(outcome.reason ?? '')) {
      // The browser died under this contact: start a new one and try this same contact once more instead of ending the batch.
      await logEvent(root, { runId, stage: 'OUTREACH_SEND', outreachId: row.id, action: 'browser-crashed-retrying', reason: outcome.reason });
      await page.close().catch(() => undefined);
      ctx = await options.reopen();
      ({ page, outcome } = await attempt(row, `outreach-${row.id}-retry`));
    }
    db.prepare('UPDATE outreach SET status=?, screenshot=?, connection_state=?, prepared_at=? WHERE id=?').run(outcome.status, outcome.screenshot ?? null, outcome.status, new Date().toISOString(), row.id);
    await logEvent(root, { runId, stage: 'OUTREACH_SEND', outreachId: row.id, jobId: row.job_id, company: row.company, contact: row.contact_name, status: outcome.status, reason: outcome.reason });
    if (outcome.status === 'READY_TO_SEND') {
      result.readyToSend++;
      if (options.dryRun) { setStatus.run('OUTREACH_READY', row.id); await page.close().catch(() => undefined); continue; }
      const prompt = options.clickSend
        ? `\nSend this invitation to ${row.contact_name} (${row.contact_title}) at ${row.company}?\n  "${row.message}"\n[y] send   [n] not now   [s] never for this person   [a] send this and all the rest   [q] stop: `
        : `Note ready for ${row.contact_name} at ${row.company}. Review the open browser tab and click Send yourself.\nDid you click Send? [y/N]: `;
      const decision = await confirmSent(prompt);
      if (options.clickSend) {
        if (decision === true) {
          const trySend = (target: Page) => sendOpenInvitation(target).catch((error): SendOutcome => ({ sent: false, reason: message(error) }));
          let sent = await trySend(page);
          if (!sent.sent && sent.dialogGone) {
            // The dialog closed while the note waited for approval (or the invitation was sent by hand). Reopen this same invitation once, for the note already approved.
            const again = await processOutreachContact(page, root, row.contact_name, row.profile_url, row.message, `outreach-${row.id}-reopened`).catch((error): ConnectResult => ({ status: 'FAILED', reason: message(error), finalUrl: page.url() }));
            if (again.status === 'READY_TO_SEND') sent = await trySend(page);
            else if (again.status === 'PENDING' || again.status === 'ALREADY_CONNECTED') {
              setStatus.run(again.status, row.id); await remember(again.status);
              if (again.status === 'PENDING') result.pending++; else result.alreadyConnected++;
              console.log(`${row.contact_name}: LinkedIn shows this person is already ${again.status === 'PENDING' ? 'invited (invitation pending)' : 'connected'}, so nothing more was sent.`);
              await page.close().catch(() => undefined);
              continue;
            } else sent = { sent: false, reason: again.reason ?? sent.reason };
          }
          await logEvent(root, { runId, stage: 'OUTREACH_SEND', outreachId: row.id, contact: row.contact_name, action: 'send', sent: sent.sent, reason: sent.reason });
          if (sent.sent) { db.prepare("UPDATE outreach SET status='SENT', sent_at=? WHERE id=?").run(new Date().toISOString(), row.id); await remember('SENT'); result.sent++; }
          else {
            setStatus.run(sent.limitReached ? 'NEEDS_USER_INPUT' : 'FAILED', row.id); result.failed++;
            console.log(`Not sent to ${row.contact_name}: ${sent.reason}`);
            if (sent.limitReached) { await page.close().catch(() => undefined); break; }
          }
        } else if (decision === 'skip') { setStatus.run('DECLINED', row.id); await remember('DECLINED'); result.declined++; }
        else { setStatus.run('OUTREACH_READY', row.id); if (decision === 'stop') { await page.close().catch(() => undefined); break; } }
      } else if (decision === true) { db.prepare("UPDATE outreach SET status='SENT', sent_at=? WHERE id=?").run(new Date().toISOString(), row.id); await remember('SENT'); result.sent++; }
      await page.close().catch(() => undefined);
      continue;
    }
    await page.close().catch(() => undefined);
    if (outcome.status === 'ALREADY_CONNECTED') { result.alreadyConnected++; await remember('ALREADY_CONNECTED'); }
    else if (outcome.status === 'PENDING') { result.pending++; await remember('PENDING'); }
    else if (outcome.status === 'NEEDS_USER_INPUT') result.needsUserInput++;
    else if (outcome.status === 'BLOCKED_SECURITY' || outcome.status === 'LOGIN_REQUIRED') { result.blocked++; budget?.recordBlock(`outreach: ${outcome.reason ?? outcome.status}`); break; }
    else result.failed++;
  }
  return result;
}
