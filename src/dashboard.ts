import type Database from 'better-sqlite3';
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import BetterSqlite from 'better-sqlite3';
import type { Config } from './config.js';
import { AGE_BATCHES, ageBatch, ageBatchIndex, locationTier, locationTierLabel } from './priority.js';
import { LinkedInBudget } from './linkedin-budget.js';

type Row = Record<string, unknown>;
export type Integrations = { job_agent_repo?: string; job_agent_dir?: string; link_repo?: string };
export type DashboardData = ReturnType<typeof collectDashboard>;

const ACTIONABLE = ['REVIEW', 'APPLICATION_READY', 'NEEDS_USER_INPUT'];
const count = (db: Database.Database, sql: string, ...args: unknown[]): number => Number((db.prepare(sql).get(...args) as { n: number }).n);

/** GitHub run history for one repository through the gh CLI; empty when gh is missing or offline (the dashboard still renders). */
function githubRuns(repo: string | undefined): Row[] {
  if (!repo) return [];
  try {
    const out = spawnSync('gh', ['run', 'list', '-R', repo, '--limit', '8', '--json', 'event,conclusion,status,createdAt,workflowName,url'], { encoding: 'utf8', timeout: 20_000 });
    return out.status === 0 ? (JSON.parse(out.stdout) as Row[]) : [];
  } catch { return []; }
}

/** What the Python Job Agent last recorded on this machine. The cloud run is the live one, so the snapshot says how old it is. */
function jobAgentSnapshot(dir: string | undefined): Row | undefined {
  if (!dir || !existsSync(join(dir, 'jobs.db'))) return undefined;
  const snapshot: Row = { dir };
  try {
    const agent = new BetterSqlite(join(dir, 'jobs.db'), { readonly: true });
    try {
      snapshot.postingsSeen = count(agent, 'SELECT COUNT(*) n FROM seen');
      snapshot.lastSeen = (agent.prepare('SELECT MAX(first_seen) v FROM seen').get() as { v: string }).v;
    } finally { agent.close(); }
    const ledger = join(dir, 'outreach_draft_ledger.json');
    if (existsSync(ledger)) snapshot.emailDrafts = Object.keys(JSON.parse(readFileSync(ledger, 'utf8')) as object).length;
  } catch (error) { snapshot.error = error instanceof Error ? error.message : String(error); }
  return snapshot;
}

/** Midnight at the start of yesterday (local time): the dashboard shows nothing older unless asked (the database keeps everything). */
export function startOfYesterday(now = Date.now()): string { const d = new Date(now); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - 1); return d.toISOString(); }

export function collectDashboard(db: Database.Database, config: Config, integrations: Integrations = {}, now = Date.now(), online = true, since: string | null = startOfYesterday(now)) {
  const after = (column: string): string => since ? ` AND ${column} >= '${since}'` : '';
  const recent = after('COALESCE(j.posted_at, j.detected_at)');
  const priority = config.search.location_priority;
  const jobs = db.prepare(`SELECT j.id, j.company, j.title, j.location, j.work_mode, j.posted_at, j.fit_score, j.status, j.job_url FROM jobs j WHERE j.evidence_mode='live'${recent}`).all() as Array<Row & { posted_at: string | null; location: string | null; work_mode: string | null; status: string }>;
  const actionable = jobs.filter((job) => ACTIONABLE.includes(job.status));
  // Work order grid: rows are age batches (freshest first), columns are locations in priority order.
  const columns = [...priority, 'Other'];
  const grid = AGE_BATCHES.map(() => columns.map(() => 0));
  for (const job of actionable) grid[ageBatchIndex(job.posted_at, now)][locationTier(job.location, job.work_mode, priority) - 1]++;
  const openJobs = actionable.slice().sort((a, b) => ageBatchIndex(a.posted_at, now) - ageBatchIndex(b.posted_at, now) || (Number(b.fit_score) || 0) - (Number(a.fit_score) || 0)).slice(0, 200)
    .map((job): Row & { batch: string; where: string } => ({ ...job, batch: ageBatch(job.posted_at, now), where: locationTierLabel(locationTier(job.location, job.work_mode, priority), priority) }));
  const dayAgo = new Date(now - 86_400_000).toISOString();
  // Notes still waiting for approval always count; everything else only from yesterday on.
  const outreach = db.prepare(`SELECT status, COUNT(*) n FROM outreach WHERE status IN ('OUTREACH_READY','FAILED')${since ? ` OR COALESCE(sent_at, created_at) >= '${since}'` : ''} GROUP BY status`).all() as Array<{ status: string; n: number }>;
  const rows = (sql: string): Row[] => db.prepare(sql).all() as Row[];
  return {
    generatedAt: new Date(now).toISOString(), priority, columns, batches: [...AGE_BATCHES], grid, since,
    kpis: {
      liveJobsSeen: jobs.length, newLast24h: count(db, "SELECT COUNT(*) n FROM jobs WHERE evidence_mode='live' AND detected_at>=?", dayAgo), actionable: actionable.length,
      readyToSubmit: count(db, `SELECT COUNT(*) n FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='READY_FOR_SUBMISSION'${recent}`),
      needsInput: count(db, `SELECT COUNT(*) n FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='NEEDS_USER_INPUT'${recent}`),
      submitted: count(db, `SELECT COUNT(*) n FROM applications WHERE status='SUBMITTED'${since ? ` AND submitted_at >= '${since}'` : ''}`), invitationsSent: outreach.find((row) => row.status === 'SENT')?.n ?? 0,
      invitationsWaiting: outreach.filter((row) => ['OUTREACH_READY', 'FAILED'].includes(row.status)).reduce((sum, row) => sum + row.n, 0),
      people: count(db, `SELECT COUNT(DISTINCT contact_id) n FROM outreach WHERE 1=1${after('COALESCE(sent_at, created_at)')}`)
    },
    jobStatus: rows(`SELECT status, COUNT(*) n FROM jobs j WHERE j.evidence_mode='live' AND j.status!='SKIPPED'${recent} GROUP BY status ORDER BY n DESC`),
    skipped: count(db, `SELECT COUNT(*) n FROM jobs j WHERE j.evidence_mode='live' AND j.status='SKIPPED'${recent}`),
    outreach: outreach as unknown as Row[],
    openJobs,
    ready: rows(`SELECT j.id, j.company, j.title, j.location, j.fit_score, j.job_url, a.application_url, a.resume FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='READY_FOR_SUBMISSION' AND j.evidence_mode='live'${recent} ORDER BY j.fit_score DESC LIMIT 100`),
    needsYou: rows(`SELECT j.id, j.company, j.title, j.location, j.fit_score, j.job_url, a.application_url, substr(COALESCE(a.last_error,''),1,160) reason,
        EXISTS(SELECT 1 FROM questions q WHERE q.job_id=j.id AND q.status='OPEN' AND q.category='MANUAL_APPLY') manual,
        (SELECT COUNT(*) FROM questions q WHERE q.job_id=j.id AND q.status='OPEN' AND COALESCE(q.category,'')!='MANUAL_APPLY') questions
      FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='NEEDS_USER_INPUT' AND j.evidence_mode='live' AND j.status NOT IN ('SUBMITTED','CLOSED_JOB','ALREADY_APPLIED')${recent} ORDER BY j.fit_score DESC LIMIT 150`),
    submittedJobs: rows(`SELECT j.company, j.title, j.job_url, a.application_url, a.submitted_at FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='SUBMITTED'${since ? ` AND a.submitted_at >= '${since}'` : ''} ORDER BY a.submitted_at DESC LIMIT 100`),
    notesWaiting: rows(`SELECT j.company, j.title, j.job_url, c.name, c.title contact_title, c.linkedin_url profile_url, o.message, o.character_count FROM outreach o JOIN jobs j ON j.id=o.job_id JOIN contacts c ON c.id=o.contact_id WHERE o.status IN ('OUTREACH_READY','FAILED') ORDER BY o.id DESC LIMIT 100`),
    recentOutreach: rows(`SELECT j.company, j.title, j.job_url, c.name, c.title contact_title, c.linkedin_url profile_url, o.status, o.sent_at, o.character_count, o.message FROM outreach o JOIN jobs j ON j.id=o.job_id JOIN contacts c ON c.id=o.contact_id WHERE 1=1${after('COALESCE(o.sent_at, o.created_at)')} ORDER BY COALESCE(o.sent_at, o.created_at) DESC LIMIT 100`),
    runs: rows(`SELECT substr(id,1,8) id, mode, status, started_at, finished_at FROM runs WHERE 1=1${after('started_at')} ORDER BY started_at DESC LIMIT 12`),
    sources: rows("SELECT name, status, discovered, substr(COALESCE(error,''),1,120) error FROM run_sources WHERE run_id=(SELECT run_id FROM run_sources ORDER BY id DESC LIMIT 1) AND status!='SUCCESS'"),
    errors: rows(`SELECT stage, substr(error,1,160) error, timestamp FROM errors WHERE 1=1${after('timestamp')} ORDER BY id DESC LIMIT 6`),
    linkedin: new LinkedInBudget(db, config.safety).describe(),
    automation: online ? { link: githubRuns(integrations.link_repo), jobAgent: githubRuns(integrations.job_agent_repo) } : { link: [] as Row[], jobAgent: [] as Row[] },
    jobAgent: jobAgentSnapshot(integrations.job_agent_dir)
  };
}

const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]!);
const ago = (iso: unknown, now: number): string => { const t = Date.parse(String(iso ?? '')); if (!Number.isFinite(t)) return '–'; const h = (now - t) / 3_600_000; return h < 1 ? `${Math.max(1, Math.round(h * 60))} min ago` : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} d ago`; };
/** Only web links are ever made clickable. */
const safeUrl = (url: unknown): string => { const text = String(url ?? ''); return /^https?:\/\//i.test(text) ? text : ''; };
const link = (text: unknown, url: unknown, title = ''): string => { const href = safeUrl(url); return href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</a>` : esc(text); };
const GOOD = new Set(['COMPLETED', 'SUCCESS', 'success', 'SENT']); const BAD = new Set(['FAILED', 'failure', 'cancelled']);
const badge = (status: unknown): string => { const s = String(status ?? ''); const kind = GOOD.has(s) ? 'good' : BAD.has(s) ? 'bad' : 'warn'; const icon = kind === 'good' ? '✓' : kind === 'bad' ? '✕' : '!'; return `<span class="badge ${kind}"><b aria-hidden="true">${icon}</b> ${esc(s.replace(/_/g, ' ').toLowerCase())}</span>`; };
const STATUS_COLORS: Record<string, string> = { NEEDS_USER_INPUT: '#d29922', APPLICATION_READY: '#3fb950', UNSUPPORTED_ATS: '#8b949e', CLOSED_JOB: '#58a6ff', FAILED: '#f85149', BLOCKED_SECURITY: '#a371f7', LOGIN_REQUIRED: '#db61a2', ALREADY_APPLIED: '#39c5cf', DUPLICATE: '#6e7681', SUBMITTED: '#2ea043', REVIEW: '#388bfd', SKIPPED: '#484f58' };
const statusColor = (status: string): string => STATUS_COLORS[status] ?? '#8b949e';

const CSS = `:root{color-scheme:dark;--bg:#0d1117;--card:#161b22;--card2:#1c2230;--line:#30363d;--ink:#e6edf3;--ink2:#8b949e;--link:#58a6ff;--good:#3fb950;--warn:#d29922;--bad:#f85149;--blue:#388bfd;--heat:56,139,253}
@media(prefers-color-scheme:light){:root:not([data-theme=dark]){color-scheme:light;--bg:#f6f8fa;--card:#ffffff;--card2:#f0f3f6;--line:#d0d7de;--ink:#1f2328;--ink2:#59636e;--link:#0969da;--good:#1a7f37;--warn:#9a6700;--bad:#cf222e;--blue:#0969da;--heat:9,105,218}}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:22px 16px 56px}h1{font-size:24px;margin:0}h2{font-size:15px;margin:0 0 4px;font-weight:600}.sub{color:var(--ink2);margin:4px 0 18px;font-size:12.5px}.hint{color:var(--ink2);font-size:12px;margin:0 0 10px}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-bottom:12px}
.big{display:block;color:inherit;text-decoration:none;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;min-height:116px}.big:hover{border-color:var(--ink2);text-decoration:none}
.big .v{font-size:40px;font-weight:600;line-height:1}.big .l{margin-top:10px;font-size:12.5px;color:var(--ink2)}.big.g{background:linear-gradient(135deg,rgba(63,185,80,.16),transparent 70%)}.big.g .v{color:var(--good)}.big.o .v{color:var(--warn)}.big.b .v{color:var(--link)}
.minis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.mini{display:block;color:inherit;text-decoration:none;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.mini:hover{border-color:var(--ink2);text-decoration:none}.mini .v{font-size:20px;font-weight:600}.mini .l{font-size:11.5px;color:var(--ink2)}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;scroll-margin-top:12px}.card:target{border-color:var(--link);box-shadow:0 0 0 1px var(--link)}
.stack{display:flex;height:12px;border-radius:6px;overflow:hidden;margin:8px 0 12px;gap:1px}.stack i{display:block;min-width:3px}
.legend{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:4px 14px;font-size:12px}.legend span{display:flex;gap:6px;align-items:center;color:var(--ink2)}.legend b{color:var(--ink);font-weight:600;margin-left:auto}.sw{width:9px;height:9px;border-radius:2px;flex:none}
.scroll{overflow:auto;max-height:360px;border-top:1px solid var(--line)}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--ink2);font-weight:500;font-size:11.5px;position:sticky;top:0;background:var(--card)}
td small{display:block;color:var(--ink2);font-size:11.5px}tr[hidden]{display:none}
.pill{display:inline-block;min-width:34px;text-align:center;padding:1px 7px;border-radius:10px;font-size:12px;background:rgba(63,185,80,.18);color:var(--good)}.pill.m{background:rgba(88,166,255,.16);color:var(--link)}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11.5px;white-space:nowrap}.badge b{margin-right:3px}.badge.good{background:rgba(63,185,80,.16);color:var(--good)}.badge.warn{background:rgba(210,153,34,.16);color:var(--warn)}.badge.bad{background:rgba(248,81,73,.16);color:var(--bad)}.badge.manual{background:rgba(88,166,255,.16);color:var(--link)}
.filter{width:100%;background:var(--bg);border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:7px 10px;margin:6px 0 8px;font:inherit}
.grid th:first-child{white-space:nowrap}.grid td.cell{text-align:center;min-width:76px;font-weight:600}.grid td.cell[data-n]{cursor:pointer;background:rgba(var(--heat),calc(var(--v)*.55 + .12))}.grid td.cell[data-n]:hover{outline:1px solid var(--link)}
button{font:inherit;font-size:12px;background:var(--card2);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:3px 10px;cursor:pointer}button:hover{border-color:var(--ink2)}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}
details.card>summary{cursor:pointer;font-weight:600;list-style:none}details.card>summary::-webkit-details-marker{display:none}details.card>summary::before{content:"▸ ";color:var(--ink2)}details[open].card>summary::before{content:"▾ "}
.msg{white-space:pre-wrap;color:var(--ink2);font-size:12px;margin:4px 0 0;max-width:640px}.muted{color:var(--ink2)}.empty{color:var(--ink2);padding:10px 0}`;

const SCRIPT = [
  "document.querySelectorAll('input[data-filter]').forEach(function(box){box.addEventListener('input',function(){var t=box.value.toLowerCase();document.querySelectorAll(box.dataset.filter+' tbody tr').forEach(function(r){r.hidden=t!==''&&!r.textContent.toLowerCase().includes(t)});});});",
  "document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){var label=b.dataset.label||b.textContent;navigator.clipboard.writeText(b.dataset.copy).then(function(){b.textContent='Copied';setTimeout(function(){b.textContent=label},1500)}).catch(function(){b.textContent=b.dataset.copy});});});",
  "document.querySelectorAll('button[data-more]').forEach(function(b){b.addEventListener('click',function(){document.querySelectorAll(b.dataset.more+' tr.extra').forEach(function(r){r.classList.remove('extra');r.hidden=false});b.remove();});});",
  "var clear=document.getElementById('clear-open');",
  "function showOpen(batch,where){document.querySelectorAll('#open-table tbody tr').forEach(function(r){r.hidden=!((!batch||r.dataset.batch===batch)&&(!where||r.dataset.where===where))});document.getElementById('open-label').textContent=batch?('showing '+batch+' / '+where):'';if(clear)clear.hidden=!batch;}",
  "document.querySelectorAll('td.cell[data-n]').forEach(function(c){c.addEventListener('click',function(){showOpen(c.dataset.batch,c.dataset.where);location.hash='sec-open';});});",
  "if(clear)clear.addEventListener('click',function(){showOpen('','')});"
].join('\n');

export function renderDashboard(data: DashboardData): string {
  const now = Date.parse(data.generatedAt); const k = data.kpis;
  const max = Math.max(1, ...data.grid.flat());
  const grid = `<div class="scroll" style="max-height:none"><table class="grid"><thead><tr><th>Posted</th>${data.columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${data.batches.map((batch, i) => `<tr><th style="position:static">${esc(batch)}</th>${data.grid[i].map((n, j) => n ? `<td class="cell" data-n="${n}" data-batch="${esc(batch)}" data-where="${esc(data.columns[j])}" style="--v:${(n / max).toFixed(2)}" title="${esc(batch)} · ${esc(data.columns[j])}: ${n} job(s) - click to list them">${n}</td>` : '<td class="cell muted">–</td>').join('')}</tr>`).join('')}</tbody></table></div>`;
  const total = data.jobStatus.reduce((sum, row) => sum + Number(row.n), 0) || 1;
  const stack = `<div class="stack">${data.jobStatus.map((row) => `<i style="width:${(Number(row.n) / total) * 100}%;background:${statusColor(String(row.status))}" title="${esc(String(row.status).replace(/_/g, ' ').toLowerCase())}: ${row.n}"></i>`).join('')}</div>`;
  const legend = `<div class="legend">${data.jobStatus.map((row) => `<span><i class="sw" style="background:${statusColor(String(row.status))}"></i>${esc(String(row.status).replace(/_/g, ' ').toLowerCase())}<b>${row.n}</b></span>`).join('')}</div>`;
  const big = (cls: string, href: string, value: number, label: string): string => `<a class="big ${cls}" href="#${href}"><div class="v">${value}</div><div class="l">${esc(label)}</div></a>`;
  const mini = (href: string, value: number, label: string): string => `<a class="mini" href="#${href}"><div class="v">${value}</div><div class="l">${esc(label)}</div></a>`;
  const table = (head: string[], body: string[][], empty: string, attrs = '', rowAttrs: string[] = []): string => body.length ? `<table ${attrs}><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${body.map((r, i) => `<tr${rowAttrs[i] ?? ''}>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>` : `<div class="empty">${esc(empty)}</div>`;
  const roleCell = (r: Row): string => `<strong>${link(r.title, r.job_url, 'Open the job on LinkedIn')}</strong><small>${esc(r.company)}${r.location ? ` · ${esc(r.location)}` : ''}</small>`;
  const copy = (command: string, label: string): string => `<button data-copy="${esc(command)}" data-label="${esc(label)}">${esc(label)}</button>`;

  const readyRows = data.ready.map((r) => [roleCell(r), `<span class="pill">${esc(r.fit_score)}</span>`, `${esc(r.resume ?? '')}${safeUrl(r.application_url) ? `<small>${link('open the application', r.application_url)}</small>` : ''}`]);
  const needsRows = data.needsYou.map((r) => [roleCell(r), `<span class="pill">${esc(r.fit_score)}</span>`,
    `${Number(r.manual) ? `<span class="badge manual">${safeUrl(r.application_url) ? link('Apply manually ↗', r.application_url, 'Open the employer page') : 'Apply manually'}</span>` : `<span class="badge warn">Answer needed${Number(r.questions) ? ` (${r.questions})` : ''}</span>`}<small>${esc(r.reason)}</small>`]);
  const needsAttrs = data.needsYou.map((_, i) => (i >= 15 ? ' class="extra" hidden' : ''));
  const needsExtra = data.needsYou.length > 15;
  const openRows = data.openJobs.map((r) => [roleCell(r), esc(r.where), esc(r.batch), `<span class="pill m">${esc(r.fit_score ?? '–')}</span>`, esc(String(r.status).replace(/_/g, ' ').toLowerCase())]);
  const openAttrs = data.openJobs.map((r) => ` data-batch="${esc(r.batch)}" data-where="${esc(r.where)}"`);
  const notesRows = data.notesWaiting.map((r) => [`<strong>${link(r.name, r.profile_url, 'Open their LinkedIn profile')}</strong><small>${esc(r.contact_title)}</small>`, `${link(r.company, r.job_url, 'Open the job')}<small>${esc(r.title)}</small>`, `<details><summary class="muted">${esc(r.character_count)} chars — read the note</summary><div class="msg">${esc(r.message)}</div></details>`]);
  const outreachRows = data.recentOutreach.map((r) => [`<strong>${link(r.name, r.profile_url, 'Open their LinkedIn profile')}</strong><small>${esc(r.contact_title)}</small>`, `${link(r.company, r.job_url, 'Open the job')}<small>${esc(r.title)}</small>`, badge(r.status), esc(ago(r.sent_at, now)), `<details><summary class="muted">${esc(r.character_count)} chars</summary><div class="msg">${esc(r.message)}</div></details>`]);
  const submittedRows = data.submittedJobs.map((r) => [`<strong>${link(r.title, r.application_url ?? r.job_url)}</strong><small>${esc(r.company)}</small>`, esc(ago(r.submitted_at, now))]);
  const runs = (items: Row[]): string => table(['Workflow', 'Trigger', 'Result', 'When'], items.map((r) => [link(r.workflowName, r.url, 'Open the run on GitHub'), esc(r.event), badge(r.conclusion || r.status), esc(ago(r.createdAt, now))]), 'No run history (gh CLI unavailable or offline).');
  const agent = data.jobAgent;
  const sinceText = data.since ? `Showing jobs and activity since yesterday (${esc(data.since.slice(0, 10))}); older data stays in the database (dashboard --all shows it)` : 'Showing everything';

  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Job Hub Dashboard</title><style>${CSS}</style></head><body><main>
<div class="row"><h1>Job Hub</h1><div>${copy('npm run dev -- inbox', 'Copy: inbox command')} ${copy('npm run dev -- outreach', 'Copy: send-notes command')}</div></div>
<p class="sub">${sinceText} · Updated ${esc(data.generatedAt.replace('T', ' ').slice(0, 16))} UTC · LinkedIn: ${esc(data.linkedin)}<br>Work order: freshest batch first, then ${esc(data.priority.join(' → '))} → other · Every number, job, person and link below is clickable.</p>
<div class="hero">${big('g', 'sec-ready', k.readyToSubmit, 'Ready to submit — you confirm')}${big('o', 'sec-needs', k.needsInput, 'Need your input')}${big('b', 'sec-notes', k.invitationsWaiting, 'Outreach notes waiting for approval')}</div>
<div class="minis">${mini('sec-open', k.liveJobsSeen, 'Live jobs seen')}${mini('sec-open', k.newLast24h, 'New in 24 h')}${mini('sec-open', k.actionable, 'Open to act on')}${mini('sec-submitted', k.submitted, 'Submitted')}${mini('sec-outreach', k.invitationsSent, 'Invitations sent')}${mini('sec-outreach', k.people, 'People contacted')}</div>
<div class="cols"><section class="card"><h2>Where every live job stands</h2>${stack}${legend}<p class="hint" style="margin-top:10px">${data.skipped} more were filtered out (wrong role, internship, under 4 years, duplicate, too old).</p></section>
<section class="card" id="sec-grid"><h2>Open jobs by age and location</h2><p class="hint">Click a number to list those jobs.</p>${grid}</section></div>
<div class="cols"><section class="card" id="sec-ready"><div class="row"><h2>Ready for you to submit <span class="muted">${data.ready.length}</span></h2>${copy('npm run dev -- inbox --only ready', 'Copy: inbox command')}</div><p class="hint">Forms are filled and nothing is submitted until you choose it in the inbox. Click a role to open the job.</p><div class="scroll">${table(['Job', 'Fit', 'Résumé'], readyRows, 'Nothing is ready.')}</div></section>
<section class="card" id="sec-needs"><div class="row"><h2>Needs your input <span class="muted">showing ${Math.min(15, data.needsYou.length)} of ${data.needsYou.length}</span></h2>${copy('npm run dev -- inbox --only questions', 'Copy: inbox command')}</div><input class="filter" data-filter="#needs-table" placeholder="Filter by company, role or reason"><div class="scroll">${table(['Job', 'Fit', "What's needed"], needsRows, 'Nothing is waiting on you.', 'id="needs-table"', needsAttrs)}</div>${needsExtra ? `<p class="hint"><button data-more="#needs-table">Show all ${data.needsYou.length}</button></p>` : ''}<p class="hint">"Apply manually" opens the employer's page. "Answer needed" is answered in the inbox.</p></section></div>
<section class="card" id="sec-open" style="margin-bottom:12px"><div class="row"><h2>Open jobs <span class="muted" id="open-label"></span></h2><button id="clear-open" hidden>Show all</button></div><input class="filter" data-filter="#open-table" placeholder="Filter open jobs"><div class="scroll">${table(['Job', 'Where', 'Posted', 'Fit', 'Status'], openRows, 'No open jobs since yesterday.', 'id="open-table"', openAttrs)}</div></section>
<div class="cols"><section class="card" id="sec-notes"><h2>Connection notes waiting for your approval <span class="muted">${data.notesWaiting.length}</span></h2><p class="hint">Each is exactly 300 characters with your signature. Approve and send with: ${copy('npm run dev -- outreach', 'Copy: send-notes command')}</p><div class="scroll">${table(['Person', 'For the job', 'Note'], notesRows, 'No notes are waiting.')}</div></section>
<section class="card" id="sec-submitted"><h2>Applications submitted <span class="muted">${data.submittedJobs.length}</span></h2><div class="scroll">${table(['Job', 'When'], submittedRows, 'Nothing submitted since yesterday.')}</div></section></div>
<section class="card" id="sec-outreach" style="margin-bottom:12px"><h2>Latest outreach <span class="muted">${data.recentOutreach.length} since yesterday · each note is 300 characters including the signature</span></h2><input class="filter" data-filter="#outreach-table" placeholder="Filter by person, company or role"><div class="scroll">${table(['Person', 'Job', 'Status', 'Sent', 'Note'], outreachRows, 'No outreach since yesterday.', 'id="outreach-table"')}</div></section>
<details class="card" id="sec-runs" style="margin-bottom:12px"><summary>Runs, source problems and errors</summary><div class="cols" style="margin:12px 0 0"><div><h2>Job Link runs <span class="muted">this machine's database</span></h2>${table(['Run', 'Kind', 'Result', 'Started'], data.runs.map((r) => [esc(r.id), esc(r.mode), badge(r.status), esc(ago(r.started_at, now))]), 'No runs since yesterday.')}</div>
<div>${data.sources.length ? `<h2>Sources with problems</h2>${table(['Source', 'Status', 'Detail'], data.sources.map((r) => [esc(r.name), badge(r.status), esc(r.error)]), '')}` : ''}${data.errors.length ? `<h2 style="margin-top:12px">Recent errors</h2>${table(['Stage', 'Error', 'When'], data.errors.map((r) => [esc(r.stage), esc(r.error), esc(ago(r.timestamp, now))]), '')}` : '<div class="empty">No errors since yesterday.</div>'}</div></div></details>
<details class="card" id="sec-automation"><summary>Automations on GitHub</summary><div class="cols" style="margin:12px 0 0"><div><h2>Job Link <span class="muted">LinkedIn, applications, outreach</span></h2>${runs(data.automation.link)}</div><div><h2>Job Agent <span class="muted">company boards, research, Gmail drafts, Drive report</span></h2>${runs(data.automation.jobAgent)}</div></div>${agent ? `<p class="hint" style="margin-top:10px">Job Agent snapshot on this machine: ${esc(agent.postingsSeen)} postings stored${agent.emailDrafts ? ` · ${esc(agent.emailDrafts)} Gmail drafts written` : ''}.</p>` : ''}</details>
</main><script>${SCRIPT}</script></body></html>`;
}

export async function writeDashboard(root: string, db: Database.Database, config: Config, online = true, all = false): Promise<string> {
  let integrations: Integrations = {};
  try { integrations = JSON.parse(readFileSync(join(root, 'config', 'integrations.json'), 'utf8')) as Integrations; } catch { /* optional */ }
  const file = join(root, 'reports', 'dashboard.html');
  await mkdir(dirname(file), { recursive: true });
  await writeFile(file, renderDashboard(collectDashboard(db, config, integrations, Date.now(), online, all ? null : startOfYesterday())), 'utf8');
  return file;
}
