import { spawnSync } from 'node:child_process';
import { createInterface } from 'node:readline/promises';
import type Database from 'better-sqlite3';
import type { Config } from './config.js';
import { isSensitiveQuestion, normalizeTypedAnswer, rememberAnswer } from './answer-memory.js';
import { ageBatch, compareByPriority, locationTier, locationTierLabel } from './priority.js';
import { transitionApplication } from './state.js';

type Job = { id: number; company: string; title: string; location: string | null; work_mode: string | null; posted_at: string | null; fit_score: number | null; job_url: string; application_url: string | null; resume: string | null; alerted: number };
type Question = { id: number; job_id: number; question: string; page_url: string | null };
type Item = { n: number; kind: 'READY' | 'MANUAL' | 'QUESTIONS'; job: Job; questions: Question[] };
export type Decision = 'SUBMIT' | 'APPLIED' | 'DROP' | 'ANSWERED';

const SKIP_STATUS = "('SUBMITTED','CLOSED_JOB','ALREADY_APPLIED','EXCLUDED','SKIPPED','DUPLICATE')";
const JOB_COLUMNS = "j.id,j.company,j.title,j.location,j.work_mode,j.posted_at,j.fit_score,j.job_url,a.application_url,a.resume,(j.metadata LIKE '%LinkedIn alert email%') alerted";
const OPEN_QUESTION = "q.status='OPEN' AND COALESCE(q.category,'')!='MANUAL_APPLY' AND COALESCE(q.field_key,'')!='submission_confirmation'";

function order(config: Config): (a: Job, b: Job) => number {
  const byPriority = compareByPriority(config.search.location_priority);
  return (a, b) => b.alerted - a.alerted || byPriority(a, b);
}
function readyJobs(db: Database.Database, config: Config): Job[] {
  return (db.prepare(`SELECT ${JOB_COLUMNS} FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='READY_FOR_SUBMISSION' AND j.evidence_mode='live' AND a.submitted_at IS NULL`).all() as Job[]).sort(order(config));
}
function manualJobs(db: Database.Database, config: Config): Job[] {
  return (db.prepare(`SELECT DISTINCT ${JOB_COLUMNS} FROM questions q JOIN jobs j ON j.id=q.job_id LEFT JOIN applications a ON a.job_id=j.id WHERE q.status='OPEN' AND q.category='MANUAL_APPLY' AND j.evidence_mode='live' AND j.status NOT IN ${SKIP_STATUS}`).all() as Job[]).sort(order(config));
}
function questionJobs(db: Database.Database, config: Config): Array<{ job: Job; questions: Question[] }> {
  const jobs = (db.prepare(`SELECT DISTINCT ${JOB_COLUMNS} FROM questions q JOIN jobs j ON j.id=q.job_id LEFT JOIN applications a ON a.job_id=j.id WHERE ${OPEN_QUESTION} AND j.evidence_mode='live' AND j.status NOT IN ${SKIP_STATUS}`).all() as Job[]).sort(order(config));
  return jobs.map((job) => ({ job, questions: db.prepare(`SELECT q.id,q.job_id,q.question,q.page_url FROM questions q WHERE q.job_id=? AND ${OPEN_QUESTION} ORDER BY q.id`).all(job.id) as Question[] }));
}

/** What is waiting, without opening anything (also used by tests). */
export function inboxCounts(db: Database.Database, config: Config): { ready: number; manual: number; questions: number; questionJobs: number } {
  const withQuestions = questionJobs(db, config);
  return { ready: readyJobs(db, config).length, manual: manualJobs(db, config).length, questions: withQuestions.reduce((n, item) => n + item.questions.length, 0), questionJobs: withQuestions.length };
}

/** "1,3,5-7", "all", "none" or empty -> the chosen numbers (only those in 1..max). */
export function parseSelection(text: string, max: number): Set<number> {
  const chosen = new Set<number>(); const input = text.trim().toLowerCase();
  if (!input || input === 'none' || input === 'no' || input === 'n') return chosen;
  if (input === 'all' || input === 'a' || input === '*') { for (let n = 1; n <= max; n++) chosen.add(n); return chosen; }
  for (const part of input.split(/[,\s]+/).filter(Boolean)) {
    const range = part.match(/^(\d+)-(\d+)$/);
    if (range) { for (let n = Number(range[1]); n <= Math.min(Number(range[2]), max); n++) if (n >= 1) chosen.add(n); continue; }
    if (/^\d+$/.test(part) && Number(part) >= 1 && Number(part) <= max) chosen.add(Number(part));
  }
  return chosen;
}

const clip = (text: string, width: number): string => (text.length > width - 1 ? `${text.slice(0, width - 2)}…` : text).padEnd(width);
const ago = (iso: string | null): string => { const t = Date.parse(iso ?? ''); if (!Number.isFinite(t)) return '?'; const h = (Date.now() - t) / 3_600_000; return h < 1 ? `${Math.max(1, Math.round(h * 60))}m` : h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`; };
const where = (job: Job, config: Config): string => locationTierLabel(locationTier(job.location, job.work_mode, config.search.location_priority), config.search.location_priority);
const line = (char = '─', width = 112): string => char.repeat(width);

function printTable(items: Item[], config: Config): void {
  console.log(`${clip('#', 4)}${clip('WHAT', 11)}${clip('COMPANY — ROLE', 52)}${clip('WHERE', 11)}${clip('AGE', 5)}${clip('FIT', 5)}WAITING FOR YOU`);
  console.log(line());
  for (const item of items) {
    const waiting = item.kind === 'READY' ? 'submit?' : item.kind === 'MANUAL' ? 'apply on the employer page' : `${item.questions.length} question(s)`;
    console.log(`${clip(String(item.n), 4)}${clip(item.kind, 11)}${clip(`${item.job.company} — ${item.job.title}${item.job.alerted ? ' ★' : ''}`, 52)}${clip(where(item.job, config), 11)}${clip(ago(item.job.posted_at), 5)}${clip(String(item.job.fit_score ?? '–'), 5)}${waiting}`);
  }
  console.log(line());
  console.log('★ = from your LinkedIn alert. Order: your alerts first, then Hyderabad, Remote, Bangalore, then newest.\n');
}

const openUrl = (url: string): void => { spawnSync('cmd', ['/c', 'start', '', url], { stdio: 'ignore' }); };
const runCli = (root: string, args: string[]): number => spawnSync('npx', ['tsx', 'src/cli.ts', ...args], { cwd: root, stdio: 'inherit', shell: true }).status ?? 1;

function drop(db: Database.Database, jobId: number): void {
  const at = new Date().toISOString();
  db.transaction(() => {
    db.prepare("UPDATE applications SET status='EXCLUDED', last_error='Dropped by you', updated_at=? WHERE job_id=? AND submitted_at IS NULL").run(at, jobId);
    db.prepare("UPDATE jobs SET status='SKIPPED', filter_reason='Dropped by you' WHERE id=?").run(jobId);
    db.prepare("UPDATE questions SET status='STALE' WHERE job_id=? AND status='OPEN'").run(jobId);
  })();
}
const savePlan = (db: Database.Database, jobId: number, decision: Decision): void => { db.prepare('INSERT INTO inbox_plan(job_id,decision,decided_at) VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET decision=excluded.decision,decided_at=excluded.decided_at,done_at=NULL,result=NULL').run(jobId, decision, new Date().toISOString()); };
const finishPlan = (db: Database.Database, jobId: number, result: string): void => { db.prepare('UPDATE inbox_plan SET done_at=?,result=? WHERE job_id=?').run(new Date().toISOString(), result, jobId); };
const status = (db: Database.Database, jobId: number): { status: string; last_error: string | null } => (db.prepare('SELECT status,last_error FROM applications WHERE job_id=?').get(jobId) as { status: string; last_error: string | null } | undefined) ?? { status: 'UNKNOWN', last_error: null };

/**
 * Step 1 shows everything waiting on you in one numbered table. Step 2 collects ALL your decisions and answers (numbers like 1,3,5-7 · all · none · y/n ·
 * numbers or text) and saves them as you go. Step 3 goes back and does the work: fills the answered applications again, then clicks Submit on the ones
 * you chose, and prints a result table. Answers are saved for next time (plain facts by name, other questions by their wording; legal/consent
 * questions are never reused).
 */
export async function runInbox(root: string, db: Database.Database, config: Config, options: { only?: string; all?: boolean; maxAgeHours?: number } = {}): Promise<void> {
  const only = options.only;
  const maxAgeHours = options.maxAgeHours ?? 72;
  // Old postings are almost always closed or filled: by default only jobs posted in the last 3 days are shown (an unknown age is kept).
  const fresh = (job: Job): boolean => options.all || !Number.isFinite(Date.parse(job.posted_at ?? '')) || Date.now() - Date.parse(job.posted_at ?? '') <= maxAgeHours * 3_600_000;
  if (!process.stdin.isTTY && process.env.JOB_LINK_ALLOW_PIPED_INBOX !== '1') throw new Error('The inbox is interactive: run it in a terminal (npm run dev -- inbox).');
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const ask = async (prompt: string): Promise<string> => (await rl.question(prompt)).trim();
  try {
    // A plan from an earlier session that was never carried out.
    const pending = db.prepare("SELECT p.job_id,p.decision,j.company,j.title FROM inbox_plan p JOIN jobs j ON j.id=p.job_id WHERE p.done_at IS NULL ORDER BY p.decided_at").all() as Array<{ job_id: number; decision: Decision; company: string; title: string }>;
    if (pending.length) {
      console.log(`\nYou have ${pending.length} saved decision(s) that were not carried out yet:`);
      for (const row of pending) console.log(`  ${clip(row.decision, 9)} ${row.company} — ${row.title}`);
      if ((await ask('Carry them out now? [y = yes, n = start fresh and keep them for later] > ')).toLowerCase() === 'y') { await execute(root, db, rl, pending.map((row) => ({ jobId: row.job_id, decision: row.decision })), false); return; }
    }

    const allReady = !only || only === 'ready' ? readyJobs(db, config) : [];
    const allManual = !only || only === 'manual' ? manualJobs(db, config) : [];
    const allQuestions = !only || only === 'questions' ? questionJobs(db, config) : [];
    const ready = allReady.filter(fresh); const manual = allManual.filter(fresh); const withQuestions = allQuestions.filter((entry) => fresh(entry.job));
    const hidden = allReady.length - ready.length + allManual.length - manual.length + allQuestions.length - withQuestions.length;
    const items: Item[] = [...ready.map((job) => ({ kind: 'READY' as const, job, questions: [] })), ...manual.map((job) => ({ kind: 'MANUAL' as const, job, questions: [] })), ...withQuestions.map((entry) => ({ kind: 'QUESTIONS' as const, ...entry }))].map((item, index) => ({ ...item, n: index + 1 }));
    console.log(`\n${line('═')}\n  INBOX — ${ready.length} ready to submit · ${manual.length} to finish on the employer's page · ${withQuestions.reduce((n, entry) => n + entry.questions.length, 0)} question(s) on ${withQuestions.length} job(s)\n${line('═')}\n`);
    if (hidden) console.log(`(${hidden} older item(s) posted more than ${Math.round(maxAgeHours / 24)} days ago are hidden: they are almost always closed. Show them with:  npm run dev -- inbox --all)
`);
    if (!items.length) { console.log('Nothing fresh is waiting on you.'); return; }
    printTable(items, config);

    const plan: Array<{ jobId: number; decision: Decision }> = [];
    const byKind = (kind: Item['kind']): Item[] => items.filter((item) => item.kind === kind);
    const numbers = (selected: Set<number>): Item[] => items.filter((item) => selected.has(item.n));

    // ---- Step 1: ready applications
    const readyItems = byKind('READY');
    if (readyItems.length) {
      console.log('STEP 1 — READY APPLICATIONS (the forms are filled; nothing has been submitted)');
      const submit = parseSelection(await ask('  Submit which?  numbers like 1,3,5-7 · all · none (Enter = none) > '), items.length);
      const dropSet = parseSelection(await ask('  Drop which (never apply)?  numbers · none (Enter = none) > '), items.length);
      for (const item of numbers(submit).filter((i) => i.kind === 'READY')) { savePlan(db, item.job.id, 'SUBMIT'); plan.push({ jobId: item.job.id, decision: 'SUBMIT' }); }
      for (const item of numbers(dropSet).filter((i) => i.kind === 'READY')) { savePlan(db, item.job.id, 'DROP'); plan.push({ jobId: item.job.id, decision: 'DROP' }); }
      console.log(`  ✓ saved: ${plan.filter((p) => p.decision === 'SUBMIT').length} to submit, ${plan.filter((p) => p.decision === 'DROP').length} to drop\n`);
    }

    // ---- Step 2: applications to finish on the employer's page
    const manualItems = byKind('MANUAL');
    if (manualItems.length) {
      console.log("STEP 2 — APPLICATIONS TO FINISH YOURSELF (the employer's page has no form I can fill)");
      const open = parseSelection(await ask('  Open which in your browser?  numbers · none > '), items.length);
      for (const item of numbers(open).filter((i) => i.kind === 'MANUAL')) openUrl(item.job.application_url ?? item.job.job_url);
      const done = parseSelection(await ask('  Which have you already applied to?  numbers · none > '), items.length);
      const dropSet = parseSelection(await ask('  Drop which?  numbers · none > '), items.length);
      for (const item of numbers(done).filter((i) => i.kind === 'MANUAL')) { savePlan(db, item.job.id, 'APPLIED'); plan.push({ jobId: item.job.id, decision: 'APPLIED' }); }
      for (const item of numbers(dropSet).filter((i) => i.kind === 'MANUAL')) { savePlan(db, item.job.id, 'DROP'); plan.push({ jobId: item.job.id, decision: 'DROP' }); }
      console.log('  ✓ saved\n');
    }

    // ---- Step 3: questions
    const questionItems = byKind('QUESTIONS');
    let autoSubmit = false; let savedAnswers = 0; let reusable = 0; let quit = false;
    if (questionItems.length) {
      console.log('STEP 3 — QUESTIONS ONLY YOU CAN ANSWER');
      console.log('  Type the answer and press Enter. y / n work for yes-no questions. Enter = answer later · s = skip this job · x = drop this job · o = open the posting · q = stop asking.');
      console.log('  Every answer is saved now; plain facts (notice period, CTC, years with a tool ...) and repeated questions are filled automatically next time.\n');
      for (const item of questionItems) {
        if (quit) break;
        console.log(`${line()}\n${item.n}. ${item.job.company} — ${item.job.title}  (${where(item.job, config)}, ${ago(item.job.posted_at)})`);
        let dropped = false; let skipped = false;
        for (const [qi, question] of item.questions.entries()) {
          const needsHuman = /captcha|recaptcha|verification code|one[- ]time (?:code|password)|\botp\b/i.test(question.question);
          const flag = needsHuman ? '  ⚠ a captcha/code cannot be typed here: press o to open the posting and do it yourself, or s to skip' : isSensitiveQuestion(question.question) ? '  ⚠ legal/consent: answered per application, not reused' : '';
          for (;;) {
            const typed = await ask(`  Q${qi + 1}/${item.questions.length}  ${question.question}${flag}\n      > `);
            const key = typed.toLowerCase();
            if (key === 'q') { quit = true; break; }
            if (key === 's') { skipped = true; break; }
            if (key === 'x') { dropped = true; break; }
            if (key === 'o') { openUrl(question.page_url ?? item.job.application_url ?? item.job.job_url); continue; }
            if (typed) {
              const answer = normalizeTypedAnswer(question.question, typed);
              db.prepare("UPDATE questions SET answer=?,status='ANSWERED',answered_at=? WHERE id=?").run(answer, new Date().toISOString(), question.id);
              const how = rememberAnswer(db, question.question, answer);
              savedAnswers++; if (how !== 'not-reused') reusable++;
              console.log(`      ✓ saved${how === 'not-reused' ? ' (this application only)' : ' (will be reused)'}`);
            }
            break;
          }
          if (quit || skipped || dropped) break;
        }
        if (dropped) { savePlan(db, item.job.id, 'DROP'); plan.push({ jobId: item.job.id, decision: 'DROP' }); console.log('  ✓ will be dropped'); continue; }
        const stillOpen = Number((db.prepare(`SELECT COUNT(*) n FROM questions q WHERE q.job_id=? AND ${OPEN_QUESTION}`).get(item.job.id) as { n: number }).n);
        if (!stillOpen && item.questions.length) { savePlan(db, item.job.id, 'ANSWERED'); plan.push({ jobId: item.job.id, decision: 'ANSWERED' }); }
      }
      if (plan.some((p) => p.decision === 'ANSWERED')) {
        autoSubmit = (await ask('\n  After I fill in your answers, submit those applications automatically when every check passes? [y/n] > ')).toLowerCase() === 'y';
      }
    }
    console.log(`\n${line('═')}\n  YOUR PLAN\n${line('═')}`);
    const count = (decision: Decision): number => plan.filter((p) => p.decision === decision).length;
    console.log(`  submit now ............ ${count('SUBMIT')}\n  fill again with your answers ${count('ANSWERED')}${autoSubmit && count('ANSWERED') ? '  (then submit automatically)' : ''}\n  you applied yourself ...... ${count('APPLIED')}\n  drop ...................... ${count('DROP')}\n  answers saved ............. ${savedAnswers} (${reusable} reused automatically from now on)`);
    if (!plan.length) { console.log('\nNothing to carry out. Your answers are saved.'); return; }
    if ((await ask('\nGo ahead and do all of this now? [y/n] > ')).toLowerCase() !== 'y') { console.log('Not now. Everything you decided is saved: run the inbox again and choose "y" at the first question to carry it out.'); return; }
    await execute(root, db, rl, plan, autoSubmit);
  } finally { rl.close(); }
}

/** Carries out the plan: drops and manual confirmations, re-fills the answered applications, then clicks Submit on the chosen ones. */
async function execute(root: string, db: Database.Database, rl: ReturnType<typeof createInterface>, plan: Array<{ jobId: number; decision: Decision }>, autoSubmit: boolean): Promise<void> {
  const results: Array<{ company: string; title: string; did: string; result: string }> = [];
  // The terminal belongs to the background command while it runs.
  const cli = (args: string[]): number => { rl.pause(); try { return runCli(root, args); } finally { rl.resume(); } };
  const label = (jobId: number): { company: string; title: string } => (db.prepare('SELECT company,title FROM jobs WHERE id=?').get(jobId) as { company: string; title: string } | undefined) ?? { company: '?', title: '?' };
  console.log(`\n${line('═')}\n  DOING IT NOW\n${line('═')}`);
  for (const step of plan.filter((p) => p.decision === 'DROP')) { drop(db, step.jobId); finishPlan(db, step.jobId, 'dropped'); results.push({ ...label(step.jobId), did: 'drop', result: 'dropped' }); }
  for (const step of plan.filter((p) => p.decision === 'APPLIED')) {
    console.log(`\nRecording that you applied: ${label(step.jobId).company}`);
    const code = cli(['applied', String(step.jobId)]);
    finishPlan(db, step.jobId, code ? 'failed to record' : 'recorded as applied'); results.push({ ...label(step.jobId), did: 'applied by you', result: code ? 'could not record' : 'recorded' });
  }
  const answered = plan.filter((p) => p.decision === 'ANSWERED');
  // An explicit review (by job) ignores the freshness window that the automatic run applies, so a job you chose to answer is not dropped as "expired".
  // With auto-submit it fills the form again and submits in one go, only if every check passes; without it, it only fills.
  for (const [index, step] of answered.entries()) {
    const info = label(step.jobId);
    transitionApplication(db, step.jobId, 'READY_FOR_REVIEW', 'USER_REVIEW'); db.prepare('UPDATE applications SET attempt_count=0 WHERE job_id=?').run(step.jobId);
    console.log(`\n[${index + 1}/${answered.length}] ${autoSubmit ? 'Filling and submitting' : 'Filling'} ${info.company} — ${info.title}`);
    cli(['review', String(step.jobId), ...(autoSubmit ? ['--submit'] : []), '--headless']);
    const after = status(db, step.jobId);
    finishPlan(db, step.jobId, after.status);
    results.push({ ...info, did: autoSubmit ? 'filled + submit' : 'filled again', result: `${after.status}${after.status !== 'SUBMITTED' && after.status !== 'READY_FOR_SUBMISSION' && after.last_error ? ` — ${String(after.last_error).slice(0, 150)}` : ''}` });
  }
  const toSubmit = plan.filter((p) => p.decision === 'SUBMIT').map((p) => p.jobId);
  for (const [index, jobId] of toSubmit.entries()) {
    const info = label(jobId); const now = status(db, jobId);
    if (now.status !== 'READY_FOR_SUBMISSION') { results.push({ ...info, did: 'submit', result: `not ready: ${now.status}${now.last_error ? ` — ${String(now.last_error).slice(0, 150)}` : ''}` }); finishPlan(db, jobId, `not ready: ${now.status}`); continue; }
    console.log(`\n[${index + 1}/${toSubmit.length}] Submitting ${info.company} — ${info.title}`);
    cli(['review', String(jobId), '--submit', '--headless']);
    const after = status(db, jobId);
    finishPlan(db, jobId, after.status);
    results.push({ ...info, did: 'submit', result: `${after.status}${after.status !== 'SUBMITTED' && after.last_error ? ` — ${String(after.last_error).slice(0, 150)}` : ''}` });
  }
  console.log(`\n${line('═')}\n  RESULT\n${line('═')}`);
  console.log(`${clip('DID', 17)}${clip('COMPANY — ROLE', 56)}RESULT`);
  for (const row of results) {
    const [head, ...more] = row.result.split(' — ');
    console.log(`${clip(row.did, 17)}${clip(`${row.company} — ${row.title}`, 56)}${head}`);
    if (more.length) console.log(`${' '.repeat(17)}↳ ${more.join(' — ')}`);
  }
  console.log(`\nAnything still waiting shows up the next time you run:  npm run dev -- inbox`);
}
