import { mkdir, writeFile } from 'node:fs/promises';
import type Database from 'better-sqlite3';

export function buildSummary(db:Database.Database):Record<string,number> {
  const result:Record<string,number>={};
  for(const row of db.prepare('SELECT status,COUNT(*) count FROM jobs GROUP BY status').all() as Array<{status:string;count:number}>)result[row.status.toLowerCase()]=row.count;
  result.applications_submitted=(db.prepare("SELECT COUNT(*) n FROM applications WHERE status='SUBMITTED'").get() as {n:number}).n;
  result.open_questions=(db.prepare("SELECT COUNT(*) n FROM questions WHERE status='OPEN'").get() as {n:number}).n;
  return result;
}
export function getRunReport(db:Database.Database,runId?:string) {
  const run= (runId?db.prepare('SELECT * FROM runs WHERE id=?').get(runId):db.prepare('SELECT * FROM runs ORDER BY started_at DESC LIMIT 1').get()) as {id:string;started_at:string;finished_at:string|null;status:string;mode:string;metrics_json:string}|undefined;
  if(!run)throw new Error('No run is recorded');
  const sources=db.prepare('SELECT name,type,status,discovered,error,url,screenshot,truncated FROM run_sources WHERE run_id=?').all(run.id);
  const jobs=db.prepare(`SELECT j.id,j.company,j.title,j.location,j.posted_at,ROUND((julianday('now')-julianday(j.posted_at))*24,1) posted_age_hours,
    r.score fit_score,r.disposition,r.age_bucket,j.fit_reason,a.resume_id,a.resume,a.resume_reason,a.ats,COALESCE(r.application_status,a.status) application_status,a.attempt_count,a.last_stage,
    COALESCE(a.last_error,r.reason,j.filter_reason) reason,j.job_url,a.final_url,a.screenshot,a.evidence,
    (SELECT GROUP_CONCAT(DISTINCT o.status) FROM outreach o WHERE o.job_id=j.id) outreach_status,
    r.queued,r.processed,r.application_opened,r.forms_inspected,r.fields_filled,r.resume_uploaded
    FROM run_jobs r JOIN jobs j ON j.id=r.job_id LEFT JOIN applications a ON a.job_id=j.id WHERE r.run_id=?
    ORDER BY r.processed DESC,r.score DESC,j.company,j.title`).all(run.id) as Array<Record<string,unknown>>;
  const count=(predicate:(job:Record<string,unknown>)=>boolean)=>jobs.filter(predicate).length;
  const stored=JSON.parse(run.metrics_json) as Record<string,unknown>;
  const metrics={...stored,sourcesAttempted:sources.length,sourcesWorking:(sources as Array<{status:string}>).filter(s=>s.status==='SUCCESS'||s.status==='PARTIAL').length,
    queued:count(j=>j.queued===1),processed:count(j=>j.processed===1),applicationsOpened:count(j=>j.application_opened===1),formsInspected:count(j=>Number(j.forms_inspected)>0),formsPopulated:count(j=>Number(j.fields_filled)>0),resumeUploads:count(j=>j.resume_uploaded===1),
    readyForSubmission:count(j=>j.processed===1&&j.application_status==='READY_FOR_SUBMISSION'),submitted:count(j=>j.processed===1&&j.application_status==='SUBMITTED'),alreadyApplied:count(j=>j.processed===1&&j.application_status==='ALREADY_APPLIED'),
    blocked:count(j=>j.processed===1&&['BLOCKED_SECURITY','LOGIN_REQUIRED','PAUSED'].includes(String(j.application_status))),needsUserInput:count(j=>j.processed===1&&j.application_status==='NEEDS_USER_INPUT'),failed:count(j=>j.processed===1&&['FAILED','RETRYABLE_TECHNICAL_FAILURE','VALIDATION_FAILURE'].includes(String(j.application_status))),
    closedSkipped:count(j=>j.application_status==='CLOSED_JOB'||j.disposition==='CLOSED_JOB'),
    eligibleReadyRate: (()=>{const eligible=jobs.filter(j=>j.processed===1&&!['CLOSED_JOB','ALREADY_APPLIED'].includes(String(j.application_status)));return eligible.length?eligible.filter(j=>['READY_FOR_SUBMISSION','SUBMITTED'].includes(String(j.application_status))).length/eligible.length:null;})(),
    outreachPrepared:(db.prepare('SELECT COUNT(*) n FROM outreach WHERE run_id=?').get(run.id) as {n:number}).n,
    outreachCompleted:(db.prepare("SELECT COUNT(*) n FROM outreach WHERE run_id=? AND status='SENT'").get(run.id) as {n:number}).n,
    completed:count(j=>j.processed===1&&['SUBMITTED','ALREADY_APPLIED'].includes(String(j.application_status)))};
  const questions=db.prepare("SELECT q.* FROM questions q JOIN run_jobs r ON r.job_id=q.job_id WHERE r.run_id=? AND q.status='OPEN'").all(run.id);
  const errors=db.prepare('SELECT * FROM errors WHERE run_id=?').all(run.id);
  return {run:{...run,metrics_json:undefined,durationSeconds:run.finished_at?(Date.parse(run.finished_at)-Date.parse(run.started_at))/1000:null},metrics,sources,jobs,questions,errors};
}
function escapeHtml(value:unknown):string{return String(value??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
export async function writeRunReport(root:string,db:Database.Database,runId:string):Promise<string> {
  const report=getRunReport(db,runId);await mkdir(`${root}/reports`,{recursive:true});
  const path=`${root}/reports/${runId}`;
  await writeFile(`${path}.json`,JSON.stringify(report,null,2));
  const columns=['id','company','title','location','posted_age_hours','fit_score','resume','ats','application_status','outreach_status','reason','job_url'];
  const html=`<!doctype html><html><head><meta charset="utf-8"><title>Job Link run ${runId}</title><style>body{font:15px system-ui;margin:32px;color:#172033;background:#f5f7fb}h1{font-size:25px}table{border-collapse:collapse;background:white;width:100%;margin:20px 0}td,th{padding:9px;text-align:left;border:1px solid #d6dfec;vertical-align:top}th{background:#e7edf5}pre{white-space:pre-wrap}input{padding:10px;width:350px}</style></head><body><h1>Job Link — ${escapeHtml(report.run.mode)} run</h1><p>${escapeHtml(runId)} | ${escapeHtml(report.run.status)} | ${escapeHtml(report.run.started_at)} to ${escapeHtml(report.run.finished_at)} | ${report.run.durationSeconds}s</p><h2>Metrics</h2><pre>${escapeHtml(JSON.stringify(report.metrics,null,2))}</pre><h2>Sources</h2><pre>${escapeHtml(JSON.stringify(report.sources,null,2))}</pre><h2>Jobs</h2><input id="filter" placeholder="Filter company, role, status or reason"><table id="jobs"><thead><tr>${columns.map(c=>`<th>${c}</th>`).join('')}</tr></thead><tbody>${report.jobs.map(j=>`<tr>${columns.map(c=>`<td>${c==='job_url'?`<a href="${escapeHtml(j[c])}" rel="noreferrer">Open job</a>`:escapeHtml(j[c])}</td>`).join('')}</tr>`).join('')}</tbody></table><h2>Open questions</h2><pre>${escapeHtml(JSON.stringify(report.questions,null,2))}</pre><h2>Errors</h2><pre>${escapeHtml(JSON.stringify(report.errors,null,2))}</pre><script>document.getElementById('filter').addEventListener('input',e=>{for(const row of document.querySelectorAll('#jobs tbody tr'))row.hidden=!row.textContent.toLowerCase().includes(e.target.value.toLowerCase())});</script></body></html>`;
  await writeFile(`${path}.html`,html);await writeFile(`${root}/reports/latest.json`,JSON.stringify(report,null,2));await writeFile(`${root}/reports/latest.html`,html);return `${path}.html`;
}
