import type Database from 'better-sqlite3';
import type { Config } from './config.js';
import { levelExclusion } from './jobs.js';
import { transitionApplication } from './state.js';

// Statuses that have not reached the employer's submit, so retiring them loses nothing that was sent.
const OPEN = ['READY_FOR_REVIEW', 'READY_FOR_SUBMISSION', 'NEEDS_USER_INPUT', 'FAILED', 'RETRYABLE_TECHNICAL_FAILURE', 'VALIDATION_FAILURE', 'UNSUPPORTED_ATS', 'BLOCKED_SECURITY', 'LOGIN_REQUIRED', 'PAUSED'];
// Of two open applications to one role, the one further along is kept.
const PROGRESS = ['READY_FOR_SUBMISSION', 'NEEDS_USER_INPUT', 'READY_FOR_REVIEW'];
const DUPLICATE_WINDOW_DAYS = 30;

export const roleKey = (company: string, title: string): string =>
  [company, title].map((value) => value.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim()).join('|');

type Row = { job_id: number; status: string; company: string; title: string; description: string; posted_at: string | null; detected_at: string | null; ledger: number };
const when = (row: Pick<Row, 'posted_at' | 'detected_at'>): number => Date.parse(row.posted_at ?? row.detected_at ?? '') || 0;
const words = (text: string | null): Set<string> => new Set((text ?? '').replace(/<[^>]+>/g, ' ').toLowerCase().replace(/[^a-z0-9]+/g, ' ').split(' ').filter((word) => word.length > 2));
/** Large employers reuse one generic title ("TEST ENGINEER L3") for different roles; a repost or a copy on a second site keeps its text
 *  (live pairs overlapped 71-100% by words). A description too short to compare counts as the same role: applying twice is the worse mistake. */
function sameRole(a: string | null, b: string | null): boolean {
  const left = words(a), right = words(b);
  if (left.size < 30 || right.size < 30) return true;
  let shared = 0; for (const word of left) if (right.has(word)) shared++;
  return shared / (left.size + right.size - shared) >= 0.6;
}

/**
 * The job already applied to (or in the queue) for the same role at the same company, if any. LinkedIn reposts a role under a new job id
 * and the same employer posts it on two career sites (live 2026-09-29: Versatile Club 6227/7203, three Welo Data roles twice each); each copy
 * would otherwise be applied to separately and ask the same questions again.
 */
export function duplicateApplicationOf(db: Database.Database, job: { id: number; company: string; title: string; description: string | null; posted_at: string | null; detected_at?: string | null }): number | undefined {
  const key = roleKey(job.company, job.title);
  const others = db.prepare(`SELECT a.job_id,j.company,j.title,j.description,j.posted_at,j.detected_at FROM applications a JOIN jobs j ON j.id=a.job_id
    WHERE a.job_id!=? AND a.status NOT IN ('CLOSED_JOB','EXCLUDED') AND j.evidence_mode='live'`).all(job.id) as Array<{ job_id: number; company: string; title: string; description: string | null; posted_at: string | null; detected_at: string | null }>;
  const days = (other: typeof others[number]): number => Math.abs(when(other) - when({ posted_at: job.posted_at, detected_at: job.detected_at ?? null })) / 86_400_000;
  return others.find((other) => roleKey(other.company, other.title) === key && days(other) <= DUPLICATE_WINDOW_DAYS && sameRole(other.description, job.description))?.job_id;
}

/** Retire open applications the current rules no longer allow: below the candidate's level, or a second application to one role. */
export function retireExcludedApplications(db: Database.Database, config: Config, runId?: string): { belowLevel: number; duplicates: number } {
  const rows = db.prepare(`SELECT a.job_id,a.status,j.company,j.title,j.description,j.posted_at,j.detected_at,
      EXISTS(SELECT 1 FROM submission_ledger l WHERE l.job_id=a.job_id) ledger
    FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.evidence_mode='live'`).all() as Row[];
  let belowLevel = 0, duplicates = 0;
  const retire = (row: Row, reason: string): void => {
    db.transaction(() => {
      transitionApplication(db, row.job_id, 'EXCLUDED', 'EXCLUDED', runId, reason);
      db.prepare("UPDATE applications SET retryable=0 WHERE job_id=?").run(row.job_id);
      db.prepare("UPDATE jobs SET status='SKIPPED',filter_reason=? WHERE id=?").run(reason, row.job_id);
      db.prepare("UPDATE questions SET status='STALE' WHERE job_id=? AND status='OPEN'").run(row.job_id);
      db.prepare("UPDATE outreach SET status='JOB_EXCLUDED' WHERE job_id=? AND status IN ('OUTREACH_READY','FAILED')").run(row.job_id);
    })();
  };
  const open = rows.filter((row) => OPEN.includes(row.status) && !row.ledger);
  const retired = new Set<number>();
  for (const row of open) {
    const reason = levelExclusion({ title: row.title, description: row.description ?? '' }, config);
    if (reason) { retire(row, reason); retired.add(row.job_id); belowLevel++; }
  }
  const groups = new Map<string, Row[]>();
  for (const row of rows) {
    if (retired.has(row.job_id) || ['CLOSED_JOB', 'EXCLUDED'].includes(row.status)) continue;
    const key = roleKey(row.company, row.title);
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    const rank = (row: Row): number => (OPEN.includes(row.status) && !row.ledger ? (PROGRESS.indexOf(row.status) + 1 || PROGRESS.length + 1) : 0);
    // Anything that reached the employer (submitted, applied, an uncertain submit) outranks every open copy; then progress, then the older posting.
    const keep = [...group].sort((a, b) => rank(a) - rank(b) || when(a) - when(b) || a.job_id - b.job_id)[0];
    for (const row of group) {
      if (row === keep || !OPEN.includes(row.status) || row.ledger) continue;
      if (Math.abs(when(row) - when(keep)) / 86_400_000 > DUPLICATE_WINDOW_DAYS || !sameRole(row.description, keep.description)) continue;
      retire(row, `Duplicate of job ${keep.job_id}: the same role at the same company`);
      duplicates++;
    }
  }
  return { belowLevel, duplicates };
}
