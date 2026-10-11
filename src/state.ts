import type Database from 'better-sqlite3';
import type { ApplicationStatus } from './core/types.js';

// EXCLUDED is terminal and reachable from every status that has not touched the employer's submit: an application the search rules no
// longer allow (internship, too little experience) or a second application to the same role is retired, never filled or listed as ready.
const transitions: Record<ApplicationStatus, ApplicationStatus[]> = {
  READY_FOR_REVIEW: ['IN_PROGRESS', 'FAILED', 'NEEDS_USER_INPUT', 'EXCLUDED'],
  IN_PROGRESS: ['CLOSED_JOB', 'UNSUPPORTED_ATS', 'VALIDATION_FAILURE', 'RETRYABLE_TECHNICAL_FAILURE', 'SUBMISSION_UNCONFIRMED', 'READY_FOR_REVIEW', 'READY_FOR_SUBMISSION', 'SUBMITTING', 'NEEDS_USER_INPUT', 'BLOCKED_SECURITY', 'LOGIN_REQUIRED', 'ALREADY_APPLIED', 'FAILED', 'PAUSED'],
  READY_FOR_SUBMISSION: ['READY_FOR_REVIEW', 'IN_PROGRESS', 'EXCLUDED'],
  SUBMITTING: ['SUBMISSION_UNCONFIRMED', 'SUBMITTED', 'NEEDS_USER_INPUT', 'BLOCKED_SECURITY'],
  CLOSED_JOB: [], UNSUPPORTED_ATS: ['READY_FOR_REVIEW', 'EXCLUDED'], VALIDATION_FAILURE: ['READY_FOR_REVIEW', 'EXCLUDED'], RETRYABLE_TECHNICAL_FAILURE: ['READY_FOR_REVIEW', 'IN_PROGRESS', 'EXCLUDED'], SUBMISSION_UNCONFIRMED: [],
  SUBMITTED: [], ALREADY_APPLIED: [], EXCLUDED: [],
  NEEDS_USER_INPUT: ['READY_FOR_REVIEW', 'EXCLUDED'], BLOCKED_SECURITY: ['READY_FOR_REVIEW', 'EXCLUDED'], LOGIN_REQUIRED: ['READY_FOR_REVIEW', 'EXCLUDED'],
  FAILED: ['READY_FOR_REVIEW', 'IN_PROGRESS', 'EXCLUDED'], PAUSED: ['READY_FOR_REVIEW', 'EXCLUDED']
};

export function transitionApplication(db: Database.Database, jobId: number, to: ApplicationStatus, stage: string, runId?: string, reason?: string): void {
  db.transaction(() => {
    const row = db.prepare('SELECT status FROM applications WHERE job_id=?').get(jobId) as {status: ApplicationStatus} | undefined;
    if (!row) throw new Error(`Application ${jobId} does not exist`);
    if (row.status !== to && !transitions[row.status]?.includes(to)) throw new Error(`Illegal application transition: ${row.status} -> ${to}`);
    const now = new Date().toISOString();
    db.prepare('UPDATE applications SET status=?, last_stage=?, last_error=?, updated_at=? WHERE job_id=?').run(to, stage, reason ?? null, now, jobId);
    db.prepare('INSERT INTO application_events(job_id,run_id,from_status,to_status,stage,reason,timestamp) VALUES(?,?,?,?,?,?,?)').run(jobId, runId ?? null, row.status, to, stage, reason ?? null, now);
  })();
}

export function claimApplication(db: Database.Database, jobId: number, runId: string, maxAttempts: number): boolean {
  return db.transaction(() => {
    const result = db.prepare(`UPDATE applications SET status='IN_PROGRESS', attempt_count=attempt_count+1,run_id=?,last_stage='OPENING',updated_at=?
      WHERE job_id=? AND (status='READY_FOR_REVIEW' OR (status IN ('FAILED','RETRYABLE_TECHNICAL_FAILURE') AND retryable=1 AND (next_retry_at IS NULL OR next_retry_at<=?)))
      AND attempt_count<? AND submitted_at IS NULL AND NOT EXISTS(SELECT 1 FROM submission_ledger WHERE job_id=?)`).run(runId, new Date().toISOString(), jobId, new Date().toISOString(), maxAttempts, jobId);
    if (result.changes) db.prepare("INSERT INTO application_events(job_id,run_id,to_status,stage,timestamp) VALUES(?,?,'IN_PROGRESS','CLAIMED',?)").run(jobId, runId, new Date().toISOString());
    return result.changes === 1;
  })();
}

export function beginSubmission(db: Database.Database, jobId: number, runId: string): void {
  db.transaction(() => {
    const row = db.prepare('SELECT status,submitted_at FROM applications WHERE job_id=?').get(jobId) as {status: string; submitted_at: string|null};
    if (row.status !== 'IN_PROGRESS' || row.submitted_at) throw new Error('Application is not in a submittable state');
    db.prepare('INSERT INTO submission_ledger(job_id,run_id,started_at) VALUES(?,?,?)').run(jobId, runId, new Date().toISOString());
    transitionApplication(db, jobId, 'SUBMITTING', 'SUBMISSION_INTENT_RECORDED', runId);
  })();
}

export function recoverInterruptedApplications(db: Database.Database): {requeued: number; ambiguous: number} {
  return db.transaction(() => {
    let requeued = 0, ambiguous = 0;
    const rows = db.prepare("SELECT job_id,status FROM applications WHERE status IN ('IN_PROGRESS','SUBMITTING')").all() as Array<{job_id:number;status:string}>;
    for (const row of rows) {
      if (row.status === 'SUBMITTING') {
        const reason = 'Interrupted after submission intent. Verify the employer confirmation before any further action; automatic retry is disabled.';
        transitionApplication(db,row.job_id,'NEEDS_USER_INPUT','RECOVERY',undefined,reason);
        addQuestion(db,row.job_id,reason,'submission_confirmation','SENSITIVE');
        ambiguous++;
      } else { transitionApplication(db,row.job_id,'READY_FOR_REVIEW','RECOVERY'); requeued++; }
    }
    db.prepare("UPDATE runs SET status='INTERRUPTED',finished_at=? WHERE status='STARTED'").run(new Date().toISOString());
    return {requeued,ambiguous};
  })();
}

export function addQuestion(db: Database.Database, jobId: number, question: string, key?: string, category = 'MISSING', url?: string): void {
  const exists = db.prepare("SELECT id FROM questions WHERE job_id=? AND question=? AND status='OPEN'").get(jobId, question);
  if (!exists) db.prepare('INSERT INTO questions(job_id,question,requires_user_input,status,field_key,category,page_url,created_at) VALUES(?,?,1,\'OPEN\',?,?,?,?)').run(jobId,question,key??null,category,url??null,new Date().toISOString());
}
