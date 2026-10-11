import { appendFile, mkdir, readFile } from 'node:fs/promises';
import type Database from 'better-sqlite3';
import { canonicalContactUrl } from './contact-sources.js';

export type LedgerStatus = 'SENT' | 'PENDING' | 'ALREADY_CONNECTED' | 'DECLINED';
export type LedgerEntry = { at: string; status: LedgerStatus; url: string; name: string; title: string; company: string; message: string; jobId?: number };

const file = (root: string): string => `${root}/data/outreach-ledger.jsonl`;
// Statuses that mean the outcome for this person is already known; nothing may move a row out of them except a person choosing to.
const KNOWN = new Set<string>(['SENT', 'PENDING', 'ALREADY_CONNECTED', 'DECLINED']);

/**
 * An append-only record of what really happened on LinkedIn, kept outside the database on purpose. The database is replaced whenever the
 * cloud state is restored, which once erased the "sent" marks for invitations that had genuinely gone out; this file survives that
 * and is re-applied afterwards, so nobody is invited twice and nobody is re-drafted.
 */
export async function recordOutreach(root: string, entry: LedgerEntry): Promise<void> {
  await mkdir(`${root}/data`, { recursive: true });
  // If a crash left a half-written last line, start on a fresh line so this entry is not glued onto (and lost with) the torn one.
  const existing = await readFile(file(root), 'utf8').catch(() => '');
  await appendFile(file(root), `${existing && !existing.endsWith('\n') ? '\n' : ''}${JSON.stringify(entry)}\n`);
}

export async function readLedger(root: string): Promise<LedgerEntry[]> {
  const text = await readFile(file(root), 'utf8').catch((error: NodeJS.ErrnoException) => { if (error.code === 'ENOENT') return ''; throw error; });
  const entries: LedgerEntry[] = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try { const entry = JSON.parse(line) as LedgerEntry; if (entry.url && KNOWN.has(entry.status)) entries.push(entry); } catch { /* a torn last line is not evidence */ }
  }
  return entries;
}

/** Re-asserts the recorded outcomes on a database, whatever state it was restored to. Idempotent. */
export function applyLedger(db: Database.Database, entries: LedgerEntry[]): { updated: number; inserted: number } {
  const latest = new Map<string, LedgerEntry>();
  for (const entry of entries) { try { latest.set(canonicalContactUrl(entry.url), entry); } catch { /* not a usable URL */ } }
  let updated = 0; let inserted = 0;
  db.transaction(() => {
    for (const [url, entry] of latest) {
      let contact = db.prepare('SELECT id FROM contacts WHERE linkedin_url = ?').get(url) as { id: number } | undefined;
      if (!contact) {
        db.prepare('INSERT INTO contacts(company,name,title,linkedin_url,relevance_score) VALUES (?,?,?,?,?)').run(entry.company, entry.name, entry.title || 'Unknown', url, 65);
        contact = db.prepare('SELECT id FROM contacts WHERE linkedin_url = ?').get(url) as { id: number };
      }
      const rows = db.prepare('SELECT id, status FROM outreach WHERE contact_id = ?').all(contact.id) as Array<{ id: number; status: string }>;
      if (!rows.length) {
        const job = (entry.jobId ? db.prepare('SELECT id FROM jobs WHERE id = ?').get(entry.jobId) : undefined) ?? db.prepare('SELECT id FROM jobs WHERE company = ? ORDER BY id DESC LIMIT 1').get(entry.company);
        if (!job) continue;
        db.prepare("INSERT OR IGNORE INTO outreach(job_id,contact_id,type,message,character_count,status,sent_at,created_at) VALUES (?,?,'CONNECTION',?,?,?,?,?)")
          .run((job as { id: number }).id, contact.id, entry.message, [...entry.message].length, entry.status, entry.status === 'SENT' ? entry.at : null, entry.at);
        inserted++;
        continue;
      }
      for (const row of rows) {
        if (KNOWN.has(row.status)) continue;
        db.prepare('UPDATE outreach SET status = ?, sent_at = ? WHERE id = ?').run(entry.status, entry.status === 'SENT' ? entry.at : null, row.id);
        updated++;
      }
    }
  })();
  return { updated, inserted };
}
