import { spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline/promises';
import type Database from 'better-sqlite3';
import type { Config } from './config.js';
import { rememberVerifiedAnswer } from './answer-memory.js';
import { compareByPriority, ageBatch, locationTier, locationTierLabel } from './priority.js';
import { transitionApplication } from './state.js';

type Job = { id: number; company: string; title: string; location: string | null; work_mode: string | null; posted_at: string | null; fit_score: number | null; job_url: string; application_url: string | null; resume: string | null; alerted: number };
type Question = { id: number; job_id: number; question: string; page_url: string | null };

const SKIP_STATUS = "('SUBMITTED','CLOSED_JOB','ALREADY_APPLIED','EXCLUDED','SKIPPED','DUPLICATE')";

function readyJobs(db: Database.Database, config: Config): Job[] {
  const rows = db.prepare(`SELECT j.id,j.company,j.title,j.location,j.work_mode,j.posted_at,j.fit_score,j.job_url,a.application_url,a.resume,(j.metadata LIKE '%LinkedIn alert email%') alerted
    FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='READY_FOR_SUBMISSION' AND j.evidence_mode='live' AND a.submitted_at IS NULL`).all() as Job[];
  return rows.sort((a, b) => b.alerted - a.alerted || compareByPriority(config.search.location_priority)(a, b));
}
function manualJobs(db: Database.Database, config: Config): Job[] {
  const rows = db.prepare(`SELECT DISTINCT j.id,j.company,j.title,j.location,j.work_mode,j.posted_at,j.fit_score,j.job_url,a.application_url,a.resume,(j.metadata LIKE '%LinkedIn alert email%') alerted
    FROM questions q JOIN jobs j ON j.id=q.job_id LEFT JOIN applications a ON a.job_id=j.id WHERE q.status='OPEN' AND q.category='MANUAL_APPLY' AND j.evidence_mode='live' AND j.status NOT IN ${SKIP_STATUS}`).all() as Job[];
  return rows.sort((a, b) => b.alerted - a.alerted || compareByPriority(config.search.location_priority)(a, b));
}
function questionJobs(db: Database.Database, config: Config): Array<{ job: Job; questions: Question[] }> {
  const jobs = db.prepare(`SELECT DISTINCT j.id,j.company,j.title,j.location,j.work_mode,j.posted_at,j.fit_score,j.job_url,a.application_url,a.resume,(j.metadata LIKE '%LinkedIn alert email%') alerted
    FROM questions q JOIN jobs j ON j.id=q.job_id LEFT JOIN applications a ON a.job_id=j.id
    WHERE q.status='OPEN' AND COALESCE(q.category,'')!='MANUAL_APPLY' AND COALESCE(q.field_key,'')!='submission_confirmation' AND j.evidence_mode='live' AND j.status NOT IN ${SKIP_STATUS}`).all() as Job[];
  jobs.sort((a, b) => b.alerted - a.alerted || compareByPriority(config.search.location_priority)(a, b));
  return jobs.map((job) => ({ job, questions: db.prepare("SELECT id,job_id,question,page_url FROM questions WHERE job_id=? AND status='OPEN' AND COALESCE(category,'')!='MANUAL_APPLY' AND COALESCE(field_key,'')!='submission_confirmation' ORDER BY id").all(job.id) as Question[] }));
}

/** What is waiting, without opening anything (also used by tests and the dashboard). */
export function inboxCounts(db: Database.Database, config: Config): { ready: number; manual: number; questions: number; questionJobs: number } {
  const withQuestions = questionJobs(db, config);
  return { ready: readyJobs(db, config).length, manual: manualJobs(db, config).length, questions: withQuestions.reduce((n, item) => n + item.questions.length, 0), questionJobs: withQuestions.length };
}

const ago = (iso: string | null): string => { const t = Date.parse(iso ?? ''); if (!Number.isFinite(t)) return 'posted: unknown'; const h = (Date.now() - t) / 3_600_000; return h < 1 ? `posted ${Math.max(1, Math.round(h * 60))} min ago` : h < 48 ? `posted ${Math.round(h)} h ago` : `posted ${Math.round(h / 24)} d ago`; };
const describe = (job: Job, config: Config): string => [`${job.company} — ${job.title}`, `  ${job.location ?? 'location unknown'} · ${locationTierLabel(locationTier(job.location, job.work_mode, config.search.location_priority), config.search.location_priority)} · ${ago(job.posted_at)} (${ageBatch(job.posted_at)}) · fit ${job.fit_score ?? '–'}${job.alerted ? ' · from your LinkedIn alert' : ''}`, job.resume ? `  résumé: ${job.resume}` : '', `  ${job.application_url ?? job.job_url}`].filter(Boolean).join('\n');

function drop(db: Database.Database, jobId: number): void {
  const at = new Date().toISOString();
  db.transaction(() => {
    db.prepare("UPDATE applications SET status='EXCLUDED', last_error='Dropped by you', updated_at=? WHERE job_id=? AND submitted_at IS NULL").run(at, jobId);
    db.prepare("UPDATE jobs SET status='SKIPPED', filter_reason='Dropped by you' WHERE id=?").run(jobId);
    db.prepare("UPDATE questions SET status='STALE' WHERE job_id=? AND status='OPEN'").run(jobId);
  })();
}
const openUrl = (url: string): void => { spawnSync('cmd', ['/c', 'start', '', url], { stdio: 'ignore' }); };
const runCli = (root: string, args: string[]): number => spawnSync('npx', ['tsx', 'src/cli.ts', ...args], { cwd: root, stdio: 'inherit', shell: true }).status ?? 1;

/**
 * Everything waiting on the person, one item at a time, nothing identified by a job number:
 * 1. applications that are filled and ready: submit (one keypress per application), open, later or drop
 * 2. applications to finish on the employer's page yourself: open, mark done, later or drop
 * 3. questions only the person can answer, grouped by job: type the answer (it is remembered for the same question next time)
 */
export async function runInbox(root: string, db: Database.Database, config: Config, only?: string): Promise<void> {
  if (!process.stdin.isTTY) throw new Error('The inbox is interactive: run it in a terminal (npm run dev -- inbox).');
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const ask = async (prompt: string): Promise<string> => (await rl.question(prompt)).trim();
  let quit = false; let answeredJobs = 0;
  const ready = !only || only === 'ready' ? readyJobs(db, config) : [];
  const manual = !only || only === 'manual' ? manualJobs(db, config) : [];
  const withQuestions = !only || only === 'questions' ? questionJobs(db, config) : [];
  console.log(`\nWaiting on you: ${ready.length} ready to submit · ${manual.length} to finish on the employer's page · ${withQuestions.reduce((n, item) => n + item.questions.length, 0)} question(s) on ${withQuestions.length} job(s). (Fresh jobs from your alerts, then Hyderabad, Remote, Bangalore first.)\n`);
  try {
    for (const [index, job] of ready.entries()) {
      if (quit) break;
      console.log(`\n[ready ${index + 1}/${ready.length}] ${describe(job, config)}`);
      for (;;) {
        const key = (await ask('  [y] submit it now   [o] open the posting   [n] later   [x] drop this job   [q] quit > ')).toLowerCase();
        if (key === 'o') { openUrl(job.application_url ?? job.job_url); continue; }
        if (key === 'q') { quit = true; break; }
        if (key === 'x') { drop(db, job.id); console.log('  dropped'); break; }
        if (key === 'y') {
          rl.pause(); const code = runCli(root, ['review', String(job.id), '--submit']); rl.resume();
          const after = db.prepare('SELECT status,last_error FROM applications WHERE job_id=?').get(job.id) as { status: string; last_error: string | null } | undefined;
          console.log(`  → ${after?.status ?? 'unknown'}${after?.last_error ? `: ${String(after.last_error).slice(0, 160)}` : ''}${code ? ` (exit ${code})` : ''}`);
          break;
        }
        if (key === 'n' || key === '') break;
      }
    }
    for (const [index, job] of manual.entries()) {
      if (quit) break;
      console.log(`\n[finish yourself ${index + 1}/${manual.length}] ${describe(job, config)}`);
      for (;;) {
        const key = (await ask('  [o] open it   [d] I applied   [n] later   [x] drop this job   [q] quit > ')).toLowerCase();
        if (key === 'o') { openUrl(job.application_url ?? job.job_url); continue; }
        if (key === 'q') { quit = true; break; }
        if (key === 'x') { drop(db, job.id); console.log('  dropped'); break; }
        if (key === 'd') { rl.pause(); runCli(root, ['applied', String(job.id)]); rl.resume(); break; }
        if (key === 'n' || key === '') break;
      }
    }
    for (const [index, item] of withQuestions.entries()) {
      if (quit) break;
      console.log(`\n[questions ${index + 1}/${withQuestions.length}] ${describe(item.job, config)}`);
      let dropped = false;
      for (const question of item.questions) {
        const answer = await ask(`  Q: ${question.question}\n     your answer (Enter = later, o = open the posting, x = drop this job, q = quit) > `);
        if (answer.toLowerCase() === 'q') { quit = true; break; }
        if (answer.toLowerCase() === 'o') { openUrl(question.page_url ?? item.job.application_url ?? item.job.job_url); continue; }
        if (answer.toLowerCase() === 'x') { drop(db, item.job.id); dropped = true; console.log('  dropped'); break; }
        if (!answer) continue;
        db.prepare("UPDATE questions SET answer=?,status='ANSWERED',answered_at=? WHERE id=?").run(answer, new Date().toISOString(), question.id);
        rememberVerifiedAnswer(db, question.question, answer);
      }
      if (dropped) continue;
      const open = Number((db.prepare("SELECT COUNT(*) n FROM questions WHERE job_id=? AND status='OPEN' AND COALESCE(category,'')!='MANUAL_APPLY' AND COALESCE(field_key,'')!='submission_confirmation'").get(item.job.id) as { n: number }).n);
      if (!open && item.questions.length) {
        // Everything for this job is answered: queue it to be filled again with the answers.
        transitionApplication(db, item.job.id, 'READY_FOR_REVIEW', 'USER_REVIEW');
        db.prepare('UPDATE applications SET attempt_count=0 WHERE job_id=?').run(item.job.id);
        answeredJobs++;
      }
    }
    if (answeredJobs && !quit) {
      const key = (await ask(`\n${answeredJobs} job(s) now have all their answers. Fill them in again now (nothing is submitted)? [y/n] > `)).toLowerCase();
      if (key === 'y') { rl.pause(); runCli(root, ['run', '--headless', '--no-discover', '--limit', String(answeredJobs + 5)]); rl.resume(); }
      else console.log('They will be filled again at the next 30-minute check.');
    }
    console.log('\nInbox done. Run it again any time: npm run dev -- inbox');
  } finally { rl.close(); }
}
