import type Database from 'better-sqlite3';
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import BetterSqlite from 'better-sqlite3';
import type { Config } from './config.js';
import { AGE_BATCHES, ageBatchIndex, locationTier } from './priority.js';
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
      snapshot.byGrade = agent.prepare("SELECT verdict grade, COUNT(*) n FROM seen WHERE verdict IN ('A','B','C') GROUP BY verdict ORDER BY verdict").all();
      snapshot.lastSeen = (agent.prepare('SELECT MAX(first_seen) v FROM seen').get() as { v: string }).v;
      snapshot.best = agent.prepare("SELECT company, title, location, verdict grade, score, url FROM seen WHERE verdict IN ('A','B') ORDER BY first_seen DESC LIMIT 8").all();
    } finally { agent.close(); }
    const ledger = join(dir, 'outreach_draft_ledger.json');
    if (existsSync(ledger)) snapshot.emailDrafts = Object.keys(JSON.parse(readFileSync(ledger, 'utf8')) as object).length;
  } catch (error) { snapshot.error = error instanceof Error ? error.message : String(error); }
  return snapshot;
}

export function collectDashboard(db: Database.Database, config: Config, integrations: Integrations = {}, now = Date.now(), online = true) {
  const priority = config.search.location_priority;
  const jobs = db.prepare(`SELECT j.id, j.company, j.title, j.location, j.work_mode, j.posted_at, j.fit_score, j.status, j.job_url FROM jobs j WHERE j.evidence_mode='live'`).all() as Array<Row & { posted_at: string | null; location: string | null; work_mode: string | null; status: string }>;
  const actionable = jobs.filter((job) => ACTIONABLE.includes(job.status));
  // Work order grid: rows are age batches (freshest first), columns are locations in priority order.
  const columns = [...priority, 'Other'];
  const grid = AGE_BATCHES.map(() => columns.map(() => 0));
  for (const job of actionable) grid[ageBatchIndex(job.posted_at, now)][locationTier(job.location, job.work_mode, priority) - 1]++;
  const dayAgo = new Date(now - 86_400_000).toISOString();
  const outreach = db.prepare('SELECT status, COUNT(*) n FROM outreach GROUP BY status').all() as Array<{ status: string; n: number }>;
  const rows = (sql: string): Row[] => db.prepare(sql).all() as Row[];
  return {
    generatedAt: new Date(now).toISOString(), priority, columns, batches: [...AGE_BATCHES], grid,
    kpis: {
      liveJobsSeen: jobs.length, newLast24h: count(db, "SELECT COUNT(*) n FROM jobs WHERE evidence_mode='live' AND detected_at>=?", dayAgo), actionable: actionable.length,
      readyToSubmit: count(db, "SELECT COUNT(*) n FROM applications WHERE status='READY_FOR_SUBMISSION'"), needsInput: count(db, "SELECT COUNT(*) n FROM applications WHERE status='NEEDS_USER_INPUT'"),
      submitted: count(db, "SELECT COUNT(*) n FROM applications WHERE status='SUBMITTED'"), invitationsSent: outreach.find((row) => row.status === 'SENT')?.n ?? 0,
      invitationsWaiting: outreach.filter((row) => ['OUTREACH_READY', 'FAILED'].includes(row.status)).reduce((sum, row) => sum + row.n, 0), people: count(db, 'SELECT COUNT(*) n FROM contacts')
    },
    jobStatus: rows("SELECT status, COUNT(*) n FROM jobs WHERE evidence_mode='live' AND status!='SKIPPED' GROUP BY status ORDER BY n DESC"),
    skipped: count(db, "SELECT COUNT(*) n FROM jobs WHERE evidence_mode='live' AND status='SKIPPED'"),
    outreach: outreach as unknown as Row[],
    ready: rows(`SELECT j.id, j.company, j.title, j.location, j.fit_score, j.job_url, a.resume FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='READY_FOR_SUBMISSION' AND j.evidence_mode='live' ORDER BY j.fit_score DESC LIMIT 15`),
    needsYou: rows(`SELECT j.id, j.company, j.title, j.location, j.fit_score, j.job_url, substr(COALESCE(a.last_error,''),1,140) reason FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='NEEDS_USER_INPUT' AND j.evidence_mode='live' AND j.status NOT IN ('SUBMITTED','CLOSED_JOB','ALREADY_APPLIED') ORDER BY j.fit_score DESC LIMIT 15`),
    recentOutreach: rows(`SELECT j.company, j.title, c.name, c.title contact_title, o.status, o.sent_at, o.character_count, o.message FROM outreach o JOIN jobs j ON j.id=o.job_id JOIN contacts c ON c.id=o.contact_id ORDER BY COALESCE(o.sent_at, o.created_at) DESC LIMIT 12`),
    runs: rows('SELECT substr(id,1,8) id, mode, status, started_at, finished_at FROM runs ORDER BY started_at DESC LIMIT 12'),
    sources: rows("SELECT name, status, discovered, substr(COALESCE(error,''),1,120) error FROM run_sources WHERE run_id=(SELECT run_id FROM run_sources ORDER BY id DESC LIMIT 1) AND status!='SUCCESS'"),
    errors: rows('SELECT stage, substr(error,1,160) error, timestamp FROM errors ORDER BY id DESC LIMIT 6'),
    linkedin: new LinkedInBudget(db, config.safety).describe(),
    automation: online ? { link: githubRuns(integrations.link_repo), jobAgent: githubRuns(integrations.job_agent_repo) } : { link: [] as Row[], jobAgent: [] as Row[] },
    jobAgent: jobAgentSnapshot(integrations.job_agent_dir)
  };
}

const esc = (value: unknown): string => String(value ?? '').replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[ch]!);
const ago = (iso: unknown, now: number): string => { const t = Date.parse(String(iso ?? '')); if (!Number.isFinite(t)) return '–'; const h = (now - t) / 3_600_000; return h < 1 ? `${Math.max(1, Math.round(h * 60))} min ago` : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} d ago`; };
const link = (text: unknown, url: unknown): string => url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)}</a>` : esc(text);
const GOOD = new Set(['COMPLETED', 'SUCCESS', 'success', 'SENT']); const BAD = new Set(['FAILED', 'failure', 'cancelled']);
const badge = (status: unknown): string => { const s = String(status ?? ''); const kind = GOOD.has(s) ? 'good' : BAD.has(s) ? 'bad' : 'warn'; const icon = kind === 'good' ? '✓' : kind === 'bad' ? '✕' : '!'; return `<span class="badge ${kind}"><b aria-hidden="true">${icon}</b> ${esc(s.replace(/_/g, ' ').toLowerCase())}</span>`; };
const table = (head: string[], body: string[][], empty: string): string => body.length ? `<div class="scroll"><table><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : (empty ? `<p class="muted">${esc(empty)}</p>` : '');

const CSS = `:root{--bg:#f6f6f4;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--line:#dddcd6;--accent:42,120,214;--good:#0a7d3c;--warn:#a05a00;--bad:#c1272d}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121211;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:#33322f;--accent:57,135,229;--good:#4cc27e;--warn:#e0a03a;--bad:#ef6a6e}}
:root[data-theme=dark]{--bg:#121211;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:#33322f;--accent:57,135,229;--good:#4cc27e;--warn:#e0a03a;--bad:#ef6a6e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1200px;margin:0 auto;padding:20px 16px 48px}h1{font-size:22px;margin:0}h2{font-size:15px;margin:0 0 10px}h2.sm{font-size:13px;color:var(--ink2);margin:12px 0 8px}.sub{color:var(--ink2);margin:2px 0 18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}.tile,.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.v{font-size:26px;font-weight:650}.l{color:var(--ink2)}.h{color:var(--ink2);font-size:12px}.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:14px;margin-bottom:14px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--ink2);font-weight:600;font-size:12px}
.grid .cell{text-align:center;min-width:80px;font-weight:600;background:rgba(var(--accent),calc(var(--v)*.85))}.grid th:first-child{white-space:nowrap}
.bar{display:grid;grid-template-columns:150px 1fr 40px;align-items:center;gap:8px;margin:3px 0}.bar i{display:block;height:10px;border-radius:0 4px 4px 0;background:rgb(var(--accent))}.bar b{text-align:right}
.badge{white-space:nowrap}.badge b{margin-right:3px}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--ink2)}a{color:rgb(var(--accent))}
.note{font-size:12px;color:var(--ink2);white-space:pre-wrap;max-width:520px}details summary{cursor:pointer;color:rgb(var(--accent))}`;

export function renderDashboard(data: DashboardData): string {
  const now = Date.parse(data.generatedAt); const k = data.kpis;
  const max = Math.max(1, ...data.grid.flat());
  const grid = `<div class="scroll"><table class="grid"><thead><tr><th>Posted</th>${data.columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${data.batches.map((batch, i) => `<tr><th>${esc(batch)}</th>${data.grid[i].map((n, j) => `<td class="cell" style="--v:${(n / max).toFixed(2)}" title="${esc(batch)} · ${esc(data.columns[j])}: ${n} job(s)">${n || ''}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  const tile = (label: string, value: number, hint = ''): string => `<div class="tile"><div class="v">${value}</div><div class="l">${esc(label)}</div>${hint ? `<div class="h">${esc(hint)}</div>` : ''}</div>`;
  const barMax = Math.max(1, ...data.jobStatus.map((row) => Number(row.n)), ...data.outreach.map((row) => Number(row.n)));
  const bars = (items: Row[]): string => items.map((row) => `<div class="bar"><span>${esc(String(row.status).replace(/_/g, ' ').toLowerCase())}</span><i style="width:${Math.max(2, (Number(row.n) / barMax) * 100)}%"></i><b>${row.n}</b></div>`).join('');
  const runs = (items: Row[]): string => table(['Workflow', 'Trigger', 'Result', 'When'], items.map((r) => [link(r.workflowName, r.url), esc(r.event), badge(r.conclusion || r.status), esc(ago(r.createdAt, now))]), 'No run history (gh CLI unavailable or offline).');
  const agent = data.jobAgent;
  const agentCard = agent ? `<section class="card"><h2>Job Agent snapshot on this machine${agent.lastSeen ? ` (last posting stored ${esc(ago(agent.lastSeen, now))}; the cloud run is the live one)` : ''}</h2><p class="muted">${esc(agent.postingsSeen)} postings stored · Gmail drafts written: ${esc(agent.emailDrafts ?? 0)} · grades: ${((agent.byGrade as Row[] | undefined) ?? []).map((g) => `${esc(g.grade)}=${esc(g.n)}`).join(', ')}</p>${table(['Company', 'Title', 'Location', 'Grade'], ((agent.best as Row[]) ?? []).map((r) => [esc(r.company), link(r.title, r.url), esc(r.location), esc(r.grade)]), 'No A/B matches stored.')}</section>` : '';
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Job Hub Dashboard</title><style>${CSS}</style></head><body><main>
<h1>Job Hub</h1><p class="sub">Updated ${esc(data.generatedAt.replace('T', ' ').slice(0, 16))} UTC · LinkedIn: ${esc(data.linkedin)} · Work order: freshest age batch first, then ${esc(data.priority.join(' → '))} → other</p>
<div class="tiles">${tile('Live jobs seen', k.liveJobsSeen)}${tile('New in 24 h', k.newLast24h)}${tile('Open to act on', k.actionable)}${tile('Ready to submit', k.readyToSubmit, 'you confirm')}${tile('Needs your input', k.needsInput)}${tile('Submitted', k.submitted)}${tile('Invitations sent', k.invitationsSent)}${tile('Notes waiting', k.invitationsWaiting, 'you approve')}${tile('People contacted', k.people)}</div>
<div class="cols"><section class="card"><h2>Open jobs by age and location</h2>${grid}</section>
<section class="card"><h2>Where every live job stands</h2>${bars(data.jobStatus)}<p class="muted">${data.skipped} more were filtered out (wrong role, internship, under 4 years, duplicate, too old).</p><h2 class="sm">Outreach</h2>${bars(data.outreach) || '<p class="muted">Nothing yet.</p>'}</section></div>
<div class="cols"><section class="card"><h2>Ready for you to submit (${k.readyToSubmit})</h2>${table(['Job', 'Fit', 'Résumé'], data.ready.map((r) => [link(`${r.company} — ${r.title}`, r.job_url), esc(r.fit_score), esc(r.resume)]), 'Nothing waiting.')}</section>
<section class="card"><h2>Needs your input (${k.needsInput})</h2>${table(['Job', 'Fit', 'Why'], data.needsYou.map((r) => [link(`${r.company} — ${r.title}`, r.job_url), esc(r.fit_score), esc(r.reason)]), 'Nothing waiting.')}</section></div>
<section class="card" style="margin-bottom:14px"><h2>Latest outreach (each note is 300 characters including the signature)</h2>${table(['Company', 'Person', 'Status', 'Chars', 'Note'], data.recentOutreach.map((r) => [esc(r.company), `${esc(r.name)}<div class="muted">${esc(r.contact_title)}</div>`, badge(r.status), esc(r.character_count), `<details><summary>show</summary><div class="note">${esc(r.message)}</div></details>`]), 'No outreach yet.')}</section>
<div class="cols"><section class="card"><h2>Job Link runs (this machine's database)</h2>${table(['Run', 'Kind', 'Result', 'Started'], data.runs.map((r) => [esc(r.id), esc(r.mode), badge(r.status), esc(ago(r.started_at, now))]), 'No runs.')}${data.sources.length ? `<h2 class="sm">Sources with problems</h2>${table(['Source', 'Status', 'Detail'], data.sources.map((r) => [esc(r.name), badge(r.status), esc(r.error)]), '')}` : ''}${data.errors.length ? `<h2 class="sm">Recent errors</h2>${table(['Stage', 'Error', 'When'], data.errors.map((r) => [esc(r.stage), esc(r.error), esc(ago(r.timestamp, now))]), '')}` : ''}</section>
<section class="card"><h2>Automations on GitHub</h2><h2 class="sm">Job Link (private): LinkedIn, applications, outreach</h2>${runs(data.automation.link)}<h2 class="sm">Job Agent: company boards, research, Gmail drafts, Drive report</h2>${runs(data.automation.jobAgent)}</section></div>
${agentCard}
</main></body></html>`;
}

export async function writeDashboard(root: string, db: Database.Database, config: Config, online = true): Promise<string> {
  let integrations: Integrations = {};
  try { integrations = JSON.parse(readFileSync(join(root, 'config', 'integrations.json'), 'utf8')) as Integrations; } catch { /* optional */ }
  const file = join(root, 'reports', 'dashboard.html');
  await mkdir(dirname(file), { recursive: true });
  await writeFile(file, renderDashboard(collectDashboard(db, config, integrations, Date.now(), online)), 'utf8');
  return file;
}
