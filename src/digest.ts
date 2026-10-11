import { createHash } from 'node:crypto';
import type Database from 'better-sqlite3';
import type { Config } from './config.js';
import { LinkedInBudget } from './linkedin-budget.js';
import { getRunReport } from './report.js';

type Row = Record<string, unknown>;
export type Digest = { markdown: string; notable: boolean; counts: Record<string, number>; fingerprint: string };
const cell = (value: unknown): string => String(value ?? '').replace(/\|/g, '\\|').replace(/\s+/g, ' ').trim();
const link = (text: unknown, url: unknown): string => (url ? `[${cell(text)}](${cell(url)})` : cell(text));
const table = (header: string[], rows: string[][]): string => rows.length ? [`| ${header.join(' | ')} |`, `| ${header.map(() => '---').join(' | ')} |`, ...rows.map((row) => `| ${row.join(' | ')} |`)].join('\n') : '';
const label = (job: Row): string => `${job.company} — ${String(job.title).split('\n')[0]}`;

/** What is waiting for a human, plus how the latest discovery run went. Open items carry over from earlier runs until they are closed. */
export function buildDigest(db: Database.Database, config: Config, runId?: string): Digest {
  // People-search and outreach create runs of their own; the summary is about the discovery pipeline.
  const pipeline = runId ?? (db.prepare("SELECT id FROM runs WHERE mode IN ('live','live-discovery') ORDER BY started_at DESC LIMIT 1").get() as { id: string } | undefined)?.id;
  const report = getRunReport(db, pipeline);
  const metrics = report.metrics as Record<string, unknown>;
  const num = (key: string): number => Number(metrics[key] ?? 0);
  const jobs = report.jobs as Row[];
  const status = (job: Row): string => String(job.application_status ?? '');

  const ready = db.prepare(`SELECT j.id,j.company,j.title,j.fit_score,j.job_url,a.resume,a.final_url FROM applications a JOIN jobs j ON j.id=a.job_id
    WHERE a.status='READY_FOR_SUBMISSION' AND j.evidence_mode='live' ORDER BY j.fit_score DESC`).all() as Row[];
  const manual = db.prepare(`SELECT DISTINCT j.id,j.company,j.title,j.fit_score,j.job_url,a.resume,a.application_url FROM questions q JOIN jobs j ON j.id=q.job_id LEFT JOIN applications a ON a.job_id=j.id
    WHERE q.status='OPEN' AND q.category='MANUAL_APPLY' AND j.evidence_mode='live' AND j.status NOT IN ('SUBMITTED','CLOSED_JOB','ALREADY_APPLIED') ORDER BY j.fit_score DESC`).all() as Row[];
  const manualIds = new Set(manual.map((job) => job.id));
  const needsInput = (db.prepare(`SELECT j.id,j.company,j.title,j.fit_score,j.job_url,a.final_url,a.last_error reason FROM applications a JOIN jobs j ON j.id=a.job_id
    WHERE a.status='NEEDS_USER_INPUT' AND j.evidence_mode='live' AND j.status NOT IN ('SUBMITTED','CLOSED_JOB','ALREADY_APPLIED') ORDER BY j.fit_score DESC`).all() as Row[]).filter((job) => !manualIds.has(job.id));
  const unhealthy = jobs.filter((job) => ['FAILED', 'BLOCKED_SECURITY', 'LOGIN_REQUIRED', 'RETRYABLE_TECHNICAL_FAILURE', 'VALIDATION_FAILURE', 'UNSUPPORTED_ATS', 'SUBMISSION_UNCONFIRMED'].includes(status(job)));
  const waiting = jobs.filter((job) => job.disposition === 'ELIGIBLE' && !status(job));
  const badSources = (report.sources as Row[]).filter((source) => source.status !== 'SUCCESS');
  const budget = new LinkedInBudget(db, config.safety);

  const counts = { discovered: num('discovered'), fresh: num('fresh8') + num('fresh24'), scored: num('scored'), eligible: num('eligible'), readyToSubmit: ready.length, manualApply: manual.length, needsInput: needsInput.length, unhealthy: unhealthy.length, sourcesFailing: badSources.length, outreachDrafted: num('outreachPrepared') };
  const lines: string[] = [`# Job Link digest — ${String(report.run.started_at).slice(0, 10)}`, '', `Run \`${String(report.run.id).slice(0, 8)}\` · **${report.run.status}** · ${report.run.mode} · ${report.run.durationSeconds ?? '?'}s`, ''];
  lines.push(table(['Discovered', 'Posted ≤24h', 'Role-matched & scored', 'Newly eligible', 'Ready to submit', 'Apply manually', 'Needs input', 'Blocked / failed', 'Outreach drafted'],
    [[counts.discovered, counts.fresh, counts.scored, counts.eligible, counts.readyToSubmit, counts.manualApply, counts.needsInput, counts.unhealthy, counts.outreachDrafted].map(String)]), '');
  lines.push(`**LinkedIn:** ${cell(budget.describe())}`, '');
  if (ready.length) lines.push(`## Ready for you to review and submit (${ready.length})`, 'Forms are filled and the résumé attached; nothing was submitted. Restore the state, open the job and confirm:', '',
    table(['Job', 'Fit', 'Résumé', 'Command'], ready.map((job) => [link(label(job), job.job_url), cell(job.fit_score), cell(job.resume), `\`npm run dev -- review ${job.id} --submit\``])), '');
  if (manual.length) lines.push(`## Apply manually (${manual.length})`, 'LinkedIn Easy Apply, or an employer page with no form to fill. The right résumé is already chosen. When done: `npm run dev -- applied <id>`.', '',
    table(['Job', 'Fit', 'Résumé', 'Apply at', 'Done'], manual.map((job) => [link(label(job), job.job_url), cell(job.fit_score), cell(job.resume), link('open', job.application_url ?? job.job_url), `\`applied ${job.id}\``])), '');
  if (needsInput.length) lines.push(`## Needs your input (${needsInput.length})`, '', table(['Job', 'Fit', 'Why'], needsInput.map((job) => [link(label(job), job.final_url ?? job.job_url), cell(job.fit_score), cell(job.reason).slice(0, 180)])), '');
  if (waiting.length) lines.push(`## Eligible, not yet processed (${waiting.length})`, '', table(['Job', 'Fit'], waiting.map((job) => [link(label(job), job.job_url), cell(job.fit_score)])), '');
  for (const [heading, statuses] of [['Manual auth / security',['BLOCKED_SECURITY','LOGIN_REQUIRED']],['Unsupported ATS',['UNSUPPORTED_ATS']],['Failed / retryable',['FAILED','RETRYABLE_TECHNICAL_FAILURE','VALIDATION_FAILURE']],['Submission unconfirmed',['SUBMISSION_UNCONFIRMED']]] as Array<[string,string[]]>) {
    const items=unhealthy.filter(job=>statuses.includes(status(job)));
    if(items.length)lines.push(`## ${heading} (${items.length})`,'',table(['Job','Status','Reason'],items.map(job=>[link(label(job),job.job_url),status(job),cell(job.reason)])),'');
  }
  lines.push(`Already applied: ${jobs.filter(job=>status(job)==='ALREADY_APPLIED').length}. Closed / skipped: ${jobs.filter(job=>status(job)==='CLOSED_JOB'||job.disposition==='CLOSED_JOB').length}. Excluded so far (internship, under your experience minimum, or a duplicate): ${(db.prepare("SELECT COUNT(*) n FROM applications WHERE status='EXCLUDED'").get() as {n:number}).n}.`,'');
  if (badSources.length) lines.push(`## Sources with problems (${badSources.length})`, '', table(['Source', 'Status', 'Detail'], badSources.map((source) => [cell(source.name), cell(source.status), cell(source.error).slice(0, 160)])), '');
  const notable = counts.readyToSubmit + counts.manualApply + counts.needsInput + waiting.length > 0;
  // Which jobs are waiting on a person (not how the run went): the notification fires only when this changes.
  const fingerprint = createHash('sha1').update(JSON.stringify([ready, manual, needsInput, waiting].map((group) => group.map((job) => job.id).sort()))).digest('hex').slice(0, 12);
  return { markdown: `${lines.join('\n').trimEnd()}\n`, notable, counts, fingerprint };
}
