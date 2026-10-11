import { loadAlertJobs, readAlertFile } from './alert-import.js';
import { auditForPublication } from './audit-public.js';
import { writeOutreachWorkbook } from './excel-export.js';
import { writeDashboard } from './dashboard.js';
import { spawn } from 'node:child_process';
import { rememberVerifiedAnswer } from './answer-memory.js';
import { retireExcludedApplications } from './exclusions.js';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { Command } from 'commander';
import type { BrowserContext } from 'playwright';
import { openDatabase } from './database.js';
import { loadConfig } from './config.js';
import { openPersistentSession, closeSession, exportSessionCookies } from './browser/session.js';
import { startRun, waitForManualLogin, acquireRunLock, confirmAction, makeSendConfirmer } from './orchestrator.js';
import { importJobs } from './discovery.js';
import type { JobInput } from './jobs.js';
import { scanResumes } from './resumes.js';
import { processEligibleJobs } from './pipeline.js';
import { processApplicationQueue } from './application-runner.js';
import { discoverConfiguredSources, loadSourceSettings } from './sources/live.js';
import { recoverInterruptedApplications, transitionApplication } from './state.js';
import { ageBucket } from './jobs.js';
import { getRunReport, writeRunReport } from './report.js';
import { prepareOutreachForJobs } from './outreach-framework.js';
import { sendPreparedOutreach } from './outreach-send.js';
import { discoverAndExportContacts } from './contacts-export.js';
import { assertOnline } from './network.js';
import { LinkedInBudget } from './linkedin-budget.js';
import { logEvent } from './logger.js';
import { applyLedger, readLedger } from './outreach-ledger.js';
import { buildDigest } from './digest.js';
import { collect, extract, open as openVault, seal, vaultPassphrase } from './vault.js';
import { fetchRemoteState, packState, pushRemoteState, unpackState } from './state-sync.js';

const root=process.cwd();
const program=new Command();
program.name('job-link').description('Persistent job discovery and verified application workflow').version('0.2.0');
program.command('init').description('Initialize storage and validate all production configuration').action(async()=>{
  await Promise.all(['data','logs','screenshots','browser-profile','reports','resumes'].map(folder=>mkdir(`${root}/${folder}`,{recursive:true})));
  await loadConfig(root);const settings=await loadSourceSettings(root);const db=openDatabase(root);
  try {const resumes=await scanResumes(root,db);console.log(`Initialized. ${resumes.length} resumes; ${[...settings.sources,...settings.browser_sources].filter(s=>s.enabled).length} enabled sources.`);}finally{db.close();}
});
program.command('resumes').description('Rescan resume PDFs (also automatic during run)').action(async()=>{const db=openDatabase(root);try{console.table(await scanResumes(root,db));}finally{db.close();}});
program.command('import <file>').description('Import a fixture JSON feed; excluded from live application queue').action(async(file:string)=>{
  const config=await loadConfig(root);const input=JSON.parse(await readFile(file,'utf8')) as JobInput[];if(!Array.isArray(input))throw new Error('Job feed must be an array');
  const db=openDatabase(root);try{const run=await startRun(root,db,'fixture');const result=importJobs(db,input,config,run.runId,'fixture');await run.finish('COMPLETED',result);console.table(result);console.log(await writeRunReport(root,db,run.runId));}finally{db.close();}
});
program.command('report').description('Latest or specified run dashboard').option('--run <id>').option('--json').action(async(options:{run?:string;json?:boolean})=>{
  const db=openDatabase(root);try{const report=getRunReport(db,options.run);if(options.json)console.log(JSON.stringify(report,null,2));else{console.log(report.run);console.table(report.metrics);console.table(report.sources);console.table(report.jobs.filter(j=>j.processed===1||j.disposition==='ELIGIBLE').map(({company,title,fit_score,resume,ats,application_status,reason})=>({company,title,fit_score,resume,ats,application_status,reason})));console.log(`Complete per-job report: ${await writeRunReport(root,db,report.run.id)}`);}}finally{db.close();}
});
program.command('login').description('Legitimate manual login using the persistent browser profile').option('--url <url>').action(async(options:{url?:string})=>{
  const release=await acquireRunLock(root);let context:BrowserContext|undefined;
  try{const urls=options.url?[options.url]:(JSON.parse(await readFile(`${root}/config/sites.json`,'utf8')) as {login_urls:string[]}).login_urls;for(const url of urls)if(!/^https?:\/\//.test(url))throw new Error('Login URL must use HTTP(S)');context=await openPersistentSession(root,false);await waitForManualLogin(context,urls);const config=await loadConfig(root);const db=openDatabase(root);try{new LinkedInBudget(db,config.safety).clearBlock();}finally{db.close();}}finally{if(context)await closeSession(context);await release();}
});

type RunOptions={login?:boolean;headless?:boolean;limit?:string;source?:string;discover?:boolean};
async function executeRun(options:RunOptions,discoveryOnly=false):Promise<void>{
  const config=await loadConfig(root);const settings=await loadSourceSettings(root);
  if(options.headless)config.application.headless=true;
  if(options.limit){const limit=Number(options.limit);if(!Number.isInteger(limit)||limit<1||limit>10000)throw new Error('--limit must be 1..10000');config.application.max_jobs_per_run=limit;}
  if(options.source){const names=options.source.split(',');const known=[...settings.sources,...settings.browser_sources].map(s=>s.name);for(const name of names)if(!known.includes(name))throw new Error(`Unknown source ${name}`);settings.sources=settings.sources.filter(s=>names.includes(s.name));settings.browser_sources=settings.browser_sources.filter(s=>names.includes(s.name));}
  // Unattended environments without a LinkedIn session must not open LinkedIn at all.
  if(process.env.JOB_LINK_SKIP_LINKEDIN==='1'||(process.env.CI&&!process.env.LINKEDIN_COOKIES_B64)){const before=settings.sources.length;settings.sources=settings.sources.filter(s=>s.type!=='linkedin');if(settings.sources.length<before)console.log('[LINKEDIN] skipped: disabled or no session cookie configured for this environment');}
  await assertOnline();
  const release=await acquireRunLock(root);const db=openDatabase(root);let context:BrowserContext|undefined;let contextPromise:Promise<BrowserContext>|undefined;const budget=new LinkedInBudget(db,config.safety);
  const getContext=async()=>{if(!contextPromise)contextPromise=openPersistentSession(root,config.application.headless).then(value=>{context=value;return value;});return contextPromise;};
  const reopenContext=async()=>{if(context)await closeSession(context).catch(()=>undefined);contextPromise=undefined;return getContext();};
  let run:Awaited<ReturnType<typeof startRun>>|undefined;const metrics:Record<string,unknown>={};
  try{
    const recovery=recoverInterruptedApplications(db);run=await startRun(root,db,discoveryOnly?'live-discovery':'live');metrics.recovery=recovery;console.log(`[LINKEDIN] ${budget.describe()}`);
    const resumes=await scanResumes(root,db);metrics.resumesRegistered=resumes.length;console.log(`[RESUMES] ${resumes.length} registered`);
    if(options.login){if(config.application.headless)throw new Error('Manual login requires headed mode');const sites=JSON.parse(await readFile(`${root}/config/sites.json`,'utf8')) as {login_urls:string[]};await waitForManualLogin(await getContext(),sites.login_urls);budget.clearBlock();}
    let failedSources=0;
    if(options.discover!==false){
      const discovery=await discoverConfiguredSources(root,config,settings,getContext,undefined,budget);
      for(const source of discovery.sources)db.prepare('INSERT INTO run_sources(run_id,name,type,status,discovered,error,url,screenshot,truncated) VALUES(?,?,?,?,?,?,?,?,?)').run(run.runId,source.name,source.type,source.status,source.discovered,source.error??null,source.url??null,source.screenshot??null,source.truncated?1:0);
      const imported=importJobs(db,discovery.jobs,config,run.runId,'live');Object.assign(metrics,imported,{ids:undefined});
      failedSources=discovery.sources.filter(s=>s.status!=='SUCCESS').length;
      console.log(`[FILTER] discovered=${imported.discovered} unique=${imported.ids.length} <=8h=${imported.fresh8} 8-24h=${imported.fresh24} stale=${imported.tooOld} unknown-date=${imported.unknownAge} duplicates=${imported.duplicates}`);
      console.log(`[SCORE] scored=${imported.scored} high=${imported.highFit} medium=${imported.mediumFit} low=${imported.lowFit} eligible=${imported.eligible}`);
    }else metrics.discoverySkipped=true;
    if(!discoveryOnly){
      const queue=await processEligibleJobs(root,db,config,run.runId);metrics.queue=queue;
      const application=await processApplicationQueue(root,db,config,run.runId,getContext,undefined,false,reopenContext,!config.application.headless);metrics.remaining=application.remaining;
      const strong=db.prepare('SELECT job_id FROM run_jobs WHERE run_id=? AND score>=?').all(run.runId,config.application.auto_apply_min_score) as Array<{job_id:number}>;
      metrics.outreach=await prepareOutreachForJobs(root,db,config,run.runId,[...new Set([...strong.map(j=>j.job_id),...application.jobIds])]);
    }
    const current=getRunReport(db,run.runId).metrics;
    const blocked=Number(current.blocked)+Number(current.needsUserInput)+Number(current.readyForSubmission);
    const status=Number(metrics.remaining)>0?'LIMIT_REACHED':blocked?'USER_ACTION_REQUIRED':Number(current.failed)||failedSources?'PARTIAL':'COMPLETED';
    await run.finish(status,metrics);console.table(getRunReport(db,run.runId).metrics);console.log(`Report: ${await writeRunReport(root,db,run.runId)}`);
  }catch(error){if(run){db.prepare('INSERT INTO errors(run_id,stage,error,timestamp) VALUES(?,?,?,?)').run(run.runId,'RUN',String(error),new Date().toISOString());await run.finish('FAILED',metrics);console.log(`Failure report: ${await writeRunReport(root,db,run.runId)}`);}throw error;}
  finally{if(context)await closeSession(context).catch(()=>undefined);db.close();await release();}
}
program.command('run').description('Discover, evaluate, queue and process a real multi-job batch').option('--login').option('--headless').option('--limit <number>').option('--source <names>','comma-separated configured source names').option('--no-discover','resume durable applications without querying sources').action(options=>executeRun(options));
program.command('discover').description('Query configured live sources, normalize, persist and score').option('--login').option('--headless').option('--source <names>').action(options=>executeRun(options,true));
program.command('questions').description('List unanswered factual questions and user-action blockers').action(()=>{const db=openDatabase(root);try{console.table(db.prepare("SELECT q.id,q.job_id,j.company,j.title,q.question,q.category,q.page_url FROM questions q JOIN jobs j ON j.id=q.job_id WHERE q.status='OPEN'").all());}finally{db.close();}});
program.command('answer <questionId> <value>').description('Save a verified answer; reuse unambiguous factual answers globally').option('--reuse','also store this exact question in application.json').action(async(questionId:string,value:string,options:{reuse?:boolean})=>{
  if(!value.trim())throw new Error('Answer cannot be empty');const db=openDatabase(root);try{const question=db.prepare("SELECT question,field_key FROM questions WHERE id=? AND status='OPEN'").get(Number(questionId)) as {question:string;field_key:string}|undefined;if(!question)throw new Error('Open question not found');if(question.field_key==='submission_confirmation')throw new Error('Use reconcile to confirm an employer submission; this cannot be answered as a form question');if(options.reuse){const path=`${root}/config/application.json`;const app=JSON.parse(await readFile(path,'utf8'));app.answers={...app.answers,[question.question]:value};await writeFile(path,JSON.stringify(app,null,2)+'\n');}db.prepare("UPDATE questions SET answer=?,status='ANSWERED',answered_at=? WHERE id=?").run(value,new Date().toISOString(),Number(questionId));rememberVerifiedAnswer(db,question.question,value);console.log('Answer saved. Run review <jobId> to retry after resolving all blockers.');}finally{db.close();}
});
program.command('review <jobId>').description('Reopen and refill one live application after answering questions').option('--submit','confirm submission of this specific job if all validations pass').option('--headless').action(async(jobIdText:string,options:{submit?:boolean;headless?:boolean})=>{
  const jobId=Number(jobIdText);if(!Number.isInteger(jobId)||jobId<1)throw new Error('jobId must be a positive integer');const config=await loadConfig(root);const release=await acquireRunLock(root);const db=openDatabase(root);let context:BrowserContext|undefined;
  try{recoverInterruptedApplications(db);await scanResumes(root,db);retireExcludedApplications(db,config);const row=db.prepare('SELECT status,last_error FROM applications WHERE job_id=?').get(jobId) as {status:string;last_error:string|null}|undefined;if(!row)throw new Error('No queued application found');if(row.status==='EXCLUDED')throw new Error(`Job ${jobId} is excluded: ${row.last_error ?? 'by your search rules'}`);if(db.prepare('SELECT job_id FROM submission_ledger WHERE job_id=?').get(jobId)||['SUBMITTED','ALREADY_APPLIED','CLOSED_JOB','SUBMISSION_UNCONFIRMED'].includes(row.status))throw new Error('Application was submitted or has an uncertain submission; automatic retry is prohibited');if(row.status!=='READY_FOR_REVIEW')transitionApplication(db,jobId,'READY_FOR_REVIEW','USER_REVIEW');db.prepare('UPDATE applications SET attempt_count=0 WHERE job_id=?').run(jobId);const run=await startRun(root,db,'live-review');const getContext=async()=>context??=await openPersistentSession(root,!!options.headless);const reopenContext=async()=>{if(context)await closeSession(context).catch(()=>undefined);context=undefined;return getContext();};try{const result=await processApplicationQueue(root,db,config,run.runId,getContext,jobId,!!options.submit,reopenContext,!options.headless);await run.finish('REVIEWED',result);console.table(getRunReport(db,run.runId).metrics);console.log(await writeRunReport(root,db,run.runId));}catch(error){await run.finish('FAILED',{error:String(error)});throw error;}}finally{if(context)await closeSession(context);db.close();await release();}
});
program.command('reconcile <jobId>').description('Record externally verified employer submission confirmation').requiredOption('--confirmation <text>').action((jobIdText:string,options:{confirmation:string})=>recordSubmission(jobIdText,options.confirmation));
program.command('recheck').description('Reopen every "needs your input" or failed application (still within the posting\'s freshness window, never submitted) so the next run reprocesses it with the current code and any saved answers').action(async()=>{
  const config=await loadConfig(root);const db=openDatabase(root);
  try{
    const rows=db.prepare(`SELECT a.job_id,j.company,j.title,j.posted_at FROM applications a JOIN jobs j ON j.id=a.job_id
      WHERE j.evidence_mode='live' AND a.status IN ('NEEDS_USER_INPUT','FAILED','RETRYABLE_TECHNICAL_FAILURE','VALIDATION_FAILURE','UNSUPPORTED_ATS') AND a.submitted_at IS NULL
      AND NOT EXISTS(SELECT 1 FROM submission_ledger l WHERE l.job_id=j.id)`).all() as Array<{job_id:number;company:string;title:string;posted_at:string}>;
    let reopened=0,expired=0;
    for(const row of rows){
      const age=ageBucket(row.posted_at,config);
      if(age==='STALE'||age==='FUTURE'||(age==='UNKNOWN'&&!config.search.allow_unknown_posted_at)){expired++;continue;}
      transitionApplication(db,row.job_id,'READY_FOR_REVIEW','USER_REVIEW');
      db.prepare('UPDATE applications SET attempt_count=0 WHERE job_id=?').run(row.job_id);
      reopened++;
    }
    console.log(`Reopened ${reopened} application(s) for the next run${expired?`; ${expired} skipped because the posting is now past its ${config.search.max_age_hours}h freshness window`:''}.`);
    if(reopened)console.log('Run `npm run dev -- run` (or the laptop script) to reprocess them; anything still genuinely unanswerable will show up again in `questions`.');
  }finally{db.close();}
});
function recordSubmission(jobIdText:string,confirmation:string):void{const options={confirmation};const db=openDatabase(root);try{const jobId=Number(jobIdText);const app=db.prepare('SELECT id FROM applications WHERE job_id=?').get(jobId);if(!app)throw new Error('Application not found');if(!options.confirmation.trim())throw new Error('Confirmation evidence is required');db.transaction(()=>{db.prepare("UPDATE applications SET status='SUBMITTED',submitted_at=?,confirmation=?,last_stage='MANUAL_CONFIRMATION',updated_at=? WHERE job_id=?").run(new Date().toISOString(),options.confirmation,new Date().toISOString(),jobId);db.prepare("UPDATE jobs SET status='SUBMITTED' WHERE id=?").run(jobId);db.prepare("UPDATE questions SET status='ANSWERED',answer=?,answered_at=? WHERE job_id=? AND (field_key='submission_confirmation' OR category='MANUAL_APPLY')").run(options.confirmation,new Date().toISOString(),jobId);db.prepare("INSERT INTO application_events(job_id,to_status,stage,reason,timestamp) VALUES(?,'SUBMITTED','MANUAL_CONFIRMATION',?,?)").run(jobId,options.confirmation,new Date().toISOString());})();console.log('Recorded; this job will never be automatically resubmitted and is off your to-do list.');}finally{db.close();}}
program.command('applied <jobId>').description('Record that you applied to a manual-apply job yourself (removes it from the digest)').action((jobIdText:string)=>recordSubmission(jobIdText,'Applied manually (confirmed by the user)'));
program.command('outreach').description('Open each drafted LinkedIn contact, fill the connection note, show it to you, and send it only when you approve (y / n / s / a / q)').option('--limit <number>').option('--dry-run','fill and show every note but never send anything').option('--headless','do not show the browser window').action(async(options:{limit?:string;dryRun?:boolean;headless?:boolean})=>{
  if(!options.dryRun&&!process.stdin.isTTY)throw new Error('Approving LinkedIn invitations needs an interactive terminal: npm run dev -- outreach (add --dry-run to only fill the notes)');
  const limit=options.limit?Number(options.limit):50;if(!Number.isInteger(limit)||limit<1||limit>500)throw new Error('--limit must be 1..500');
  const release=await acquireRunLock(root);const db=openDatabase(root);let context:BrowserContext|undefined;
  let run:Awaited<ReturnType<typeof startRun>>|undefined;
  try{
    run=await startRun(root,db,'outreach-send');
    context=await openPersistentSession(root,!!options.headless);
    const config=await loadConfig(root);const budget=new LinkedInBudget(db,config.safety);if(budget.status()==='BLOCKED')throw new Error(budget.describe());console.log(`[LINKEDIN] ${budget.describe()}`);
    const result=await sendPreparedOutreach(root,db,run.runId,context,makeSendConfirmer(),limit,budget,{clickSend:true,dryRun:options.dryRun,reopen:async()=>{if(context)await closeSession(context).catch(()=>undefined);context=await openPersistentSession(root,!!options.headless);return context;}});
    await run.finish('COMPLETED',result);console.table(result);
    if(!options.dryRun){const sheet=await writeOutreachWorkbook(root,db,config.search.location_priority);console.log(`Excel: ${sheet.completed} job(s) with finished outreach, ${sheet.inProgress} still waiting -> ${sheet.file}`);}
    console.log(options.dryRun?'Dry run: every note was filled and shown; nothing was sent.':'An invitation is sent only after you approve that exact note.');
  }catch(error){
    // A crash used to leave the run marked "started" with the reason only on the screen; keep it in the log and the run record.
    const reason=error instanceof Error?error.message:String(error);
    await logEvent(root,{runId:run?.runId,stage:'OUTREACH_SEND',action:'error',error:reason}).catch(()=>undefined);
    await run?.finish('FAILED',{error:reason}).catch(()=>undefined);
    throw error;
  }finally{if(context)await closeSession(context);db.close();await release();}
});
program.command('export-linkedin-session').description('Export the logged-in LinkedIn cookies from the local browser profile as base64 for the LINKEDIN_COOKIES_B64 secret (values are never printed)').option('--out <file>','output file, relative to the project','data/linkedin-session.b64').option('--check','only verify that a logged-in session exists and report when it expires; writes nothing').action(async(options:{out:string;check?:boolean})=>{
  const release=await acquireRunLock(root);let context:BrowserContext|undefined;
  try{context=await openPersistentSession(root,true);const session=await exportSessionCookies(context);if(!session.hasSession)throw new Error('No logged-in LinkedIn session (li_at) is stored in the browser profile; run: npm run dev -- login');if(options.check){console.log(`Logged-in LinkedIn session found (${session.count} cookies)${session.sessionExpires?`; the session cookie expires ${session.sessionExpires}`:''}. Nothing was written.`);return;}const file=resolve(root,options.out);await mkdir(dirname(file),{recursive:true});await writeFile(file,session.encoded);console.log(`Exported ${session.count} LinkedIn cookies to ${options.out}${session.sessionExpires?`; the session cookie expires ${session.sessionExpires}`:''}.`);console.log('Treat this file like a password. Store it with: gh secret set LINKEDIN_COOKIES_B64 < '+options.out);}
  finally{if(context)await closeSession(context);await release();}
});
program.command('vault-seal <out> <paths...>').description('Encrypt files or folders (relative to the project) into one vault file that is safe to commit; passphrase from the VAULT_PASSPHRASE environment variable').action(async(out:string,paths:string[])=>{const blob=seal(await collect(root,paths),vaultPassphrase());await mkdir(dirname(resolve(root,out)),{recursive:true});await writeFile(resolve(root,out),blob);console.log(`Sealed ${paths.join(', ')} -> ${out} (${blob.length} bytes)`);});
program.command('vault-open <file>').description('Decrypt a vault file back into the project folder').action(async(file:string)=>{const written=await extract(root,openVault(await readFile(resolve(root,file)),vaultPassphrase()));console.log(`Restored ${written.length} files: ${written.join(', ')}`);});
program.command('state-pull').description('Restore the latest encrypted database from the "state" branch (an existing local database needs --force and is backed up first)').option('--force').action(async(options:{force?:boolean})=>{const blob=await fetchRemoteState(root);if(!blob){console.log('No remote state has been published yet; starting fresh.');return;}console.log(`Restored: ${(await unpackState(root,blob,vaultPassphrase(),!!options.force)).join(', ')}`);const restoredDb=openDatabase(root);try{const healed=applyLedger(restoredDb,await readLedger(root));if(healed.updated||healed.inserted)console.log(`Re-applied ${healed.updated+healed.inserted} recorded LinkedIn invitation outcome(s) from data/outreach-ledger.jsonl.`);}finally{restoredDb.close();}});
program.command('state-push').description('Publish the local database and contacts to the "state" branch, encrypted').action(async()=>{const blob=await packState(root,vaultPassphrase());console.log(`State published (${blob.length} bytes, commit ${(await pushRemoteState(root,blob)).slice(0,8)})`);});
program.command('digest').description('Markdown summary of the latest (or given) run: what to review, submit, apply to manually, and what is unhealthy').option('--run <id>').option('--json','print only {notable, counts}').action(async(options:{run?:string;json?:boolean})=>{const config=await loadConfig(root);const db=openDatabase(root);try{const digest=buildDigest(db,config,options.run);console.log(options.json?JSON.stringify({notable:digest.notable,counts:digest.counts,fingerprint:digest.fingerprint}):digest.markdown);}finally{db.close();}});
program.command('linkedin-status').description('Show the LinkedIn page budget for today and whether LinkedIn is paused after a block').action(async()=>{const config=await loadConfig(root);const db=openDatabase(root);try{const budget=new LinkedInBudget(db,config.safety);console.log(budget.describe());console.log(budget.unlimited()?'No daily page limit is set (linkedin_daily_page_limit is 0); pages are still spaced out.':`Remaining today: ${budget.remaining()} of ${config.safety.linkedin_daily_page_limit}`);}finally{db.close();}});
program.command('linkedin-reset').description('Clear a LinkedIn pause after you have resolved the block yourself (does not reset the daily page count)').action(async()=>{const config=await loadConfig(root);const db=openDatabase(root);try{new LinkedInBudget(db,config.safety).clearBlock();console.log('LinkedIn pause cleared.');}finally{db.close();}});
program.command('find-contacts').description('Read-only LinkedIn search for hiring-relevant people at companies with real eligible jobs; exports config/contacts.json and reports/contacts-export.csv').option('--companies <number>').option('--company <names...>','search these companies instead of the ones with eligible jobs').option('--headless').action(async(options:{companies?:string;company?:string[];headless?:boolean})=>{
  const limit=options.companies?Number(options.companies):5;if(!Number.isInteger(limit)||limit<1||limit>50)throw new Error('--companies must be 1..50');
  const release=await acquireRunLock(root);const db=openDatabase(root);let context:BrowserContext|undefined;
  try{
    const config=await loadConfig(root);const budget=new LinkedInBudget(db,config.safety);console.log(`[LINKEDIN] ${budget.describe()}`);
    if(budget.status()!=='OK'){console.log('Skipping the people search: LinkedIn is not available right now.');return;}
    const run=await startRun(root,db,'people-search');
    context=await openPersistentSession(root,!!options.headless);
    const result=await discoverAndExportContacts(root,db,run.runId,context,limit,undefined,budget,options.company);
    // Drafts only: notes for the people just found (and any already known) at companies with strong live matches. Nothing is sent.
    const strong=db.prepare("SELECT id FROM jobs WHERE evidence_mode='live' AND status IN ('REVIEW','APPLICATION_READY','NEEDS_USER_INPUT','SUBMITTED') AND fit_score>=?").all(config.application.review_min_score) as Array<{id:number}>;
    const outreach=await prepareOutreachForJobs(root,db,config,run.runId,strong.map(job=>job.id));
    await run.finish(result.blocked?'USER_ACTION_REQUIRED':'COMPLETED',{...result,outreach});
    console.table({...result,outreachDrafted:outreach.prepared});
    if(result.blocked)console.log(`Stopped early: ${result.blocked}`);
    console.log(`Open ${result.csvPath} in Excel to reach out yourself, or run "outreach" to review each drafted note and send the ones you approve. Nothing was connected, messaged, or sent by this command.`);
  }finally{if(context)await closeSession(context);db.close();await release();}
});
program.command('export-excel').description('Write reports/job-outreach.xlsx: jobs whose outreach is finished, jobs still waiting, and everyone written to').action(async()=>{const config=await loadConfig(root);const db=openDatabase(root);try{const sheet=await writeOutreachWorkbook(root,db,config.search.location_priority);console.log(`${sheet.completed} completed job(s), ${sheet.inProgress} in progress, ${sheet.people} people -> ${sheet.file}`);}finally{db.close();}});
program.command('dashboard').description('Write reports/dashboard.html: one page for jobs, applications, outreach, runs and both GitHub automations').option('--open','open it in the browser').option('--offline','skip the GitHub run history').action(async(options:{open?:boolean;offline?:boolean})=>{const config=await loadConfig(root);const db=openDatabase(root);try{const file=await writeDashboard(root,db,config,!options.offline);console.log(`Dashboard: ${file}`);if(options.open)spawn('cmd',['/c','start','',file],{detached:true,stdio:'ignore'}).unref();}finally{db.close();}});
program.command('audit-public').description('Before making the repository public: scan every file that would be published for your own details and for credentials (values are never printed)').action(async()=>{const result=await auditForPublication(root);console.log(`Checked ${result.checked} file(s).`);if(!result.findings.length){console.log('Nothing private found: safe to publish.');return;}for(const finding of result.findings)console.log(`  ${finding.file}: ${finding.kind}`);console.log(`${result.findings.length} problem(s). Do NOT publish until they are fixed.`);process.exitCode=1;});
program.command('import-alerts <file>').description('Add the LinkedIn jobs from your alert emails (file from: python run.py mailbox-export): each is opened once, scored, and queued like any other job').option('--headless').option('--limit <number>').action(async(file:string,options:{headless?:boolean;limit?:string})=>{
  const config=await loadConfig(root);await assertOnline();
  const release=await acquireRunLock(root);const db=openDatabase(root);let context:BrowserContext|undefined;let run:Awaited<ReturnType<typeof startRun>>|undefined;
  try{
    const alerts=await readAlertFile(file);console.log(`[ALERTS] ${alerts.length} LinkedIn job(s) in the alert emails`);
    run=await startRun(root,db,'live-discovery');const budget=new LinkedInBudget(db,config.safety);
    if(budget.status()==='BLOCKED')throw new Error(budget.describe());console.log(`[LINKEDIN] ${budget.describe()}`);
    context=await openPersistentSession(root,!!options.headless);
    const loaded=await loadAlertJobs(root,db,context,alerts,budget,options.limit?Number(options.limit):40);
    const imported=importJobs(db,loaded.jobs,config,run.runId,'live');
    console.log(`[ALERTS] new=${loaded.jobs.length} already-known=${loaded.skippedKnown} unreadable=${loaded.failed}${loaded.stopped?' (LinkedIn page budget reached)':''} eligible=${imported.eligible}. Next: npm run dev -- run (it queues, fills and drafts notes for them).`);
    await run.finish('COMPLETED',{alerts:alerts.length,...loaded,jobs:undefined,eligible:imported.eligible});
  }catch(error){await run?.finish('FAILED',{error:error instanceof Error?error.message:String(error)}).catch(()=>undefined);throw error;}
  finally{if(context)await closeSession(context).catch(()=>undefined);db.close();await release();}
});
program.command('draft-outreach').description('Re-write the connection notes that have not been sent yet, for the contacts already found (does not open LinkedIn)').action(async()=>{
  const config=await loadConfig(root);const release=await acquireRunLock(root);const db=openDatabase(root);
  try{
    const run=await startRun(root,db,'outreach-draft');
    const removed=db.prepare("DELETE FROM outreach WHERE status IN ('OUTREACH_READY','FAILED')").run().changes;
    const strong=db.prepare("SELECT id FROM jobs WHERE evidence_mode='live' AND status IN ('REVIEW','APPLICATION_READY','NEEDS_USER_INPUT','SUBMITTED') AND fit_score>=?").all(config.application.review_min_score) as Array<{id:number}>;
    const outreach=await prepareOutreachForJobs(root,db,config,run.runId,strong.map(job=>job.id));
    await run.finish('COMPLETED',{removed,outreach});
    console.log(`Replaced ${removed} unsent draft(s) with ${outreach.prepared} new one(s). Review them with: npm run dev -- outreach --dry-run`);
  }finally{db.close();await release();}
});
await program.parseAsync().catch(error=>{console.error(error instanceof Error?error.message:String(error));process.exitCode=1;});
// Everything has finished and been released by now; if a dead browser still pins the event loop, do not hang here.
setTimeout(()=>process.exit(process.exitCode??0),10_000).unref();
