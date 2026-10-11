import { createInterface } from 'node:readline/promises';
import { loadAnswerMemory, rememberVerifiedAnswer } from './answer-memory.js';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { BrowserContext } from 'playwright';
import type Database from 'better-sqlite3';
import type { Config } from './config.js';
import type { ApplicationStatus, CandidateAnswer, FormField, Job } from './core/types.js';
import { processApplicationPage, type ApplicationResult } from './applications.js';
import { buildCandidateAnswers, normalizeQuestion } from './ats/answers.js';
import { beginSubmission, claimApplication, addQuestion, transitionApplication } from './state.js';
import { ageBucket } from './jobs.js';
import { logEvent } from './logger.js';
import { applyKindOf, isLinkedInUrl } from './sources/normalize.js';
import { OFFLINE_ERROR } from './network.js';
import { LinkedInBudget, reservePage } from './linkedin-budget.js';
import { retireExcludedApplications } from './exclusions.js';
import { compareByPriority } from './priority.js';

/** A manual handoff owns the tab until the person closes it; the queue must stay idle. */
export async function holdManualApplication(page: import('playwright').Page, status: ApplicationStatus, interactive: boolean): Promise<void> {
  if(status!=='NEEDS_USER_INPUT'||!interactive||page.isClosed())return;
  await page.waitForEvent('close',{timeout:0});
}

/**
 * After a manual pause, what the person set in the browser becomes this application's answers (and, for plain facts, remembered answers).
 * One label can name several controls (an education "From" is a month and a year dropdown, live 2026-09-30): keyed by the label alone, the
 * year overwrote the month and was then refilled into the month dropdown. Such controls are told apart by their first option ("Month"/
 * "Year"); with nothing to tell them apart they are left as the person set them. Neither kind becomes a remembered fact. Returns what was saved.
 */
export function learnVerifiedAnswers(db:Database.Database,jobId:number,paused:Pick<ApplicationResult,'fields'|'questions'>,fields:FormField[],answers:Record<string,CandidateAnswer>):string[]{
  const saved:string[]=[];
  for(const field of fields){
    if(!field.value?.trim()||field.valid===false||field.type==='file'||field.classification==='SECURITY')continue;
    const old=paused.fields?.find(f=>f.label===field.label);
    if(!paused.questions.some(q=>q.question===field.label)&&old?.value===field.value)continue;
    const value=field.options?.find(o=>o.value===field.value)?.label??field.value;
    const shared=fields.filter(other=>normalizeQuestion(other.label)===normalizeQuestion(field.label)).length>1;
    const qualifier=shared?(field.options?.[0]?` [${normalizeQuestion(field.options[0].label)}]`:undefined):'';
    if(qualifier===undefined)continue;
    answers[`${normalizeQuestion(field.label)}${qualifier}`]={value,confidence:'configured'};
    if(!shared)rememberVerifiedAnswer(db,field.label,value);
    saved.push(`${field.label}${qualifier} = ${value}`);
    db.prepare("UPDATE questions SET answer=?,status='ANSWERED',answered_at=? WHERE job_id=? AND question=? AND status='OPEN'").run(value,new Date().toISOString(),jobId,field.label);
  }
  return saved;
}

const BROWSER_CLOSED=/Target crashed|browser has been closed|Target page, context or browser/i;
type QueueRow={job_id:number;company:string;title:string;location:string;work_mode:string;job_url:string;source:string;posted_at:string;description:string;application_url:string;external_id:string|null;ats_job_id:string|null;metadata:string|null;resume:string;fit_score:number;status:ApplicationStatus;attempt_count:number};
export async function processApplicationQueue(root:string,db:Database.Database,config:Config,runId:string,getContext:()=>Promise<BrowserContext>,onlyJobId?:number,confirmSubmission=false,reopen?:()=>Promise<BrowserContext>,manualIntervention=false):Promise<{processed:number;remaining:number;jobIds:number[]}> {
  const retired=retireExcludedApplications(db,config,runId);
  if(retired.belowLevel||retired.duplicates)await logEvent(root,{runId,stage:'APPLICATION_QUEUE',action:'excluded',...retired});
  const rows=db.prepare(`SELECT a.job_id,j.company,j.title,j.location,j.work_mode,j.job_url,j.source,j.posted_at,j.description,j.external_id,j.ats_job_id,j.metadata,j.fit_score,a.application_url,a.resume,a.status,a.attempt_count
    FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.evidence_mode='live'
    AND (a.status='READY_FOR_REVIEW' OR (a.status IN ('FAILED','RETRYABLE_TECHNICAL_FAILURE') AND a.retryable=1 AND (a.next_retry_at IS NULL OR a.next_retry_at<=?)))
    AND a.attempt_count<? AND a.submitted_at IS NULL AND NOT EXISTS(SELECT 1 FROM submission_ledger l WHERE l.job_id=j.id)
    ${onlyJobId?'AND j.id=?':''}
    `).all(new Date().toISOString(),config.application.max_attempts,...(onlyJobId?[onlyJobId]:[])) as QueueRow[];
  // Freshest age batch first, then Hyderabad / Remote / Bangalore / other, then fit; Easy Apply (the quick ones) ahead of the rest only within that order.
  const byPriority=compareByPriority(config.search.location_priority);
  rows.sort((a,b)=>byPriority(a,b)||Number(b.metadata?.includes('easy_apply')??false)-Number(a.metadata?.includes('easy_apply')??false));
  const limit=onlyJobId?1:config.application.max_jobs_per_run;
  const linkedin=new LinkedInBudget(db,config.safety);
  let processed=0;const jobIds:number[]=[];
  for(let batch=0;batch<rows.length&&processed<limit;batch+=config.application.batch_size) {
    for(const row of rows.slice(batch,batch+config.application.batch_size)) {
      if(processed>=limit)break;
      const age=ageBucket(row.posted_at,config);
      // An explicit review of one job is the person's own call on its age (the freshness window is for picking new jobs to apply to).
      if(!onlyJobId&&(age==='STALE'||age==='FUTURE'||(age==='UNKNOWN'&&!config.search.allow_unknown_posted_at))) {
        db.prepare("UPDATE jobs SET status='SKIPPED',filter_reason='Posting expired before application attempt' WHERE id=?").run(row.job_id);
        transitionApplication(db,row.job_id,'FAILED','EXPIRED',runId,'Posting expired before application attempt');continue;
      }
      // A LinkedIn job whose apply kind discovery could not read (live 2026-09-30: 10 of 92) is opened once, as a paced LinkedIn page, and the
      // page decides: Easy Apply is filled, a closed or applied posting is recorded, anything else stays a manual apply.
      const easyApply=isLinkedInUrl(row.application_url)&&['easy_apply',undefined].includes(applyKindOf(row.metadata));
      if(easyApply&&linkedin.status()!=='OK')continue; // LinkedIn is paused: the application stays queued for a later run
      if(!claimApplication(db,row.job_id,runId,config.application.max_attempts))continue;
      processed++;jobIds.push(row.job_id);
      db.prepare('INSERT INTO run_jobs(run_id,job_id,processed) VALUES(?,?,1) ON CONFLICT(run_id,job_id) DO UPDATE SET processed=1').run(runId,row.job_id);
      await logEvent(root,{runId,stage:`APPLICATION ${processed}/${Math.min(rows.length,limit)}`,jobId:row.job_id,company:row.company,title:row.title,resume:row.resume});
      let page:import('playwright').Page|undefined;
      const attempt=async():Promise<ApplicationResult>=>{
        if(isLinkedInUrl(row.application_url)&&!easyApply)throw new ManualApplyError(row.application_url);
        if(easyApply&&!await reservePage(linkedin,'easy-apply',row.application_url))throw new Error('LinkedIn is paused; this Easy Apply will be retried on a later run');
        const context=await getContext();page=await context.newPage();
        page.setDefaultTimeout(10000);
        const response=await page.goto(row.application_url,{waitUntil:'domcontentloaded',timeout:config.application.page_timeout_ms});
        db.prepare('UPDATE run_jobs SET application_opened=1 WHERE run_id=? AND job_id=?').run(runId,row.job_id);
        if(response&&response.status()>=500)throw new Error(`Retryable HTTP ${response.status()} opening application`);
        // A LinkedIn-discovered job applies on the employer's own site: the numeric LinkedIn id can never appear there, so identity
        // rests on the landing page's role heading and company name (still verified before anything is filled), not on that id.
        const external=(()=>{try{return (JSON.parse(row.metadata??'{}') as {applyKind?:string}).applyKind==='external';}catch{return false;}})();
        if(external)await page.waitForLoadState('networkidle',{timeout:8000}).catch(()=>undefined);
        const job:Job={id:row.job_id,company:row.company,title:row.title,location:row.location,workMode:row.work_mode,jobUrl:row.job_url,source:row.source,postedAt:row.posted_at,description:row.description,applicationUrl:external?page.url():row.application_url,externalId:external?undefined:row.external_id??undefined,atsJobId:external?undefined:row.ats_job_id??undefined};
        const answers=buildCandidateAnswers(config);
        loadAnswerMemory(db,answers);
        for(const answer of db.prepare("SELECT question,answer FROM questions WHERE job_id=? AND status='ANSWERED' AND answer IS NOT NULL").all(row.job_id) as Array<{question:string;answer:string}>) answers[normalizeQuestion(answer.question)]={value:answer.answer,confidence:'configured'};
        const resumePath=resolve(root,'resumes',row.resume);
        if(!resumePath.startsWith(resolve(root,'resumes')+requireSeparator()))throw new Error('Resume path escapes configured resume folder');
        return processApplicationPage(page,root,{jobId:row.job_id,job,resumePath,candidateAnswers:answers},{allowSubmit:confirmSubmission||(config.application.auto_submit&&!config.application.require_submission_confirmation&&row.fit_score>=config.application.auto_apply_min_score),onUserInput:async(result,adapter)=>{
          // Persist the stopping point before yielding control. No browser reads/writes while the person types.
          db.prepare('UPDATE applications SET evidence=?,final_url=?,screenshot=?,last_stage=? WHERE job_id=?').run(JSON.stringify({...result,pausedAt:new Date().toISOString()}),page!.url(),result.screenshot??null,'MANUAL_PAUSE',row.job_id);
          for(const q of result.questions)addQuestion(db,row.job_id,q.question,q.fieldName,q.classification,page!.url());
          if(!manualIntervention)return false;
          console.log(`[PAUSED job ${row.job_id}] ${result.questions.map(q=>q.question).join(' | ')}`);
          if(!process.stdin.isTTY){console.log('Manual answer needed; run review in an interactive terminal after answering.');return false;}
          // "skip" lets the tool close the tab itself: closing the browser window instead ends the whole browser (live 2026-09-30: four
          // relaunches in one run), and closing the tab is still honoured.
          const terminal=createInterface({input:process.stdin,output:process.stdout});
          let choice='';
          try{
            while(!['continue','c','skip','s'].includes(choice)&&!page!.isClosed())
              choice=(await Promise.race([terminal.question('Fill in or correct the fields in the browser, then type continue (or skip to leave this job for later): '),page!.waitForEvent('close',{timeout:0}).then(()=> '')])).trim().toLowerCase();
          }finally{terminal.close();}
          if(page!.isClosed())return false;
          if(choice.startsWith('s')){console.log(`[SKIPPED job ${row.job_id}] Left for later; its questions stay in the questions list.`);await page!.close().catch(()=>undefined);return false;}
          const current=await adapter.inspect(page!,{jobId:row.job_id,job,resumePath,candidateAnswers:answers});
          const learned=learnVerifiedAnswers(db,row.job_id,result,current.fields,answers);
          for(const line of learned)console.log(`  saved: ${line}`);
          const advanced=current.fields.length>0 && result.questions.every(question=>!current.fields.some(field=>field.label===question.question));
          return learned.length>0 || advanced;
        },beforeSubmit:async()=>{
          const latest=db.prepare('SELECT application_url,resume FROM applications WHERE job_id=?').get(row.job_id) as {application_url:string;resume:string};
          if(latest.application_url!==row.application_url||latest.resume!==row.resume)throw new Error('Application identity or selected resume changed during processing');
          beginSubmission(db,row.job_id,runId);
        }});
      };
      const toResult=async(error:unknown):Promise<ApplicationResult>=>{
        const ledger=db.prepare('SELECT job_id FROM submission_ledger WHERE job_id=?').get(row.job_id);
        const manualMessage=error instanceof ManualApplyError?error.message:undefined;
        const failure:ApplicationResult={status:ledger?'SUBMISSION_UNCONFIRMED':manualMessage!==undefined?'UNSUPPORTED_ATS':'RETRYABLE_TECHNICAL_FAILURE',ats:manualMessage!==undefined?'linkedin':'unknown',fieldsFilled:0,resumeUploaded:false,formsInspected:0,questions:[],reason:manualMessage??String(error),finalUrl:page?.url()??row.application_url,submissionAttempted:!!ledger};
        if(page){await mkdir(`${root}/screenshots`,{recursive:true});const screenshot=`${root}/screenshots/${Date.now()}_job-${row.job_id}_error.png`;try{await page.screenshot({path:screenshot,fullPage:true,timeout:10000});failure.screenshot=screenshot;}catch{}}
        return failure;
      };
      // A crash can surface two ways: thrown straight out of attempt() (e.g. context.newPage()/page.goto()), or absorbed by
      // processApplicationPage's own catch and returned as a normal FAILED result (e.g. a `.count()` call deep inside it) —
      // a returned result is the common case, since almost everything here runs through that function. Both are checked the
      // same way. Never retry once a submit click may have already landed (submissionAttempted): the outcome is unknown,
      // and clicking again could submit a second time.
      let result=await attempt().catch(toResult);
      if(reopen&&!result.submissionAttempted&&BROWSER_CLOSED.test(result.reason??'')){
        await logEvent(root,{runId,stage:'APPLICATION',jobId:row.job_id,action:'browser-crashed-retrying',reason:result.reason});
        await page?.close().catch(()=>undefined);page=undefined;
        await reopen();
        result=await attempt().catch(toResult);
      }
      // Once a person takes over, the queue must not close this tab or open another job underneath them.
      if (manualIntervention && process.stdin.isTTY && result.status==='NEEDS_USER_INPUT' && page && !page.isClosed()) console.log(`[PAUSED job ${row.job_id}] Close the application tab when you are finished; the queue will wait.`);
      if(page)await holdManualApplication(page,result.status,manualIntervention && !!process.stdin.isTTY);
      const ledger=!!db.prepare('SELECT job_id FROM submission_ledger WHERE job_id=?').get(row.job_id);
      if(easyApply&&(result.status==='LOGIN_REQUIRED'||result.status==='BLOCKED_SECURITY'))linkedin.recordBlock(`easy apply: ${result.reason??result.status}`);
      if(ledger&&!['SUBMITTED','BLOCKED_SECURITY','SUBMISSION_UNCONFIRMED'].includes(result.status))result.status='SUBMISSION_UNCONFIRMED';
      const retryable=['FAILED','RETRYABLE_TECHNICAL_FAILURE'].includes(result.status)&&!ledger&&(/timeout|ERR_CONNECTION|ERR_NETWORK|HTTP 5\d\d|Target crashed|browser has been closed|Target page, context or browser/i.test(result.reason??'')||OFFLINE_ERROR.test(result.reason??''));
      db.transaction(()=>{
        transitionApplication(db,row.job_id,result.status,'RESULT',runId,result.reason);
        db.prepare(`UPDATE applications SET ats=?,final_url=?,screenshot=?,confirmation=?,evidence=?,retryable=?,next_retry_at=?,submitted_at=CASE WHEN ?='SUBMITTED' THEN ? ELSE submitted_at END WHERE job_id=?`).run(result.ats,result.finalUrl,result.screenshot??null,result.confirmation??null,JSON.stringify(result),retryable?1:0,retryable?new Date(Date.now()+config.application.retry_delay_seconds*1000).toISOString():null,result.status,new Date().toISOString(),row.job_id);
        db.prepare('UPDATE jobs SET status=? WHERE id=?').run(['READY_FOR_SUBMISSION','READY_FOR_REVIEW'].includes(result.status)?'APPLICATION_READY':result.status,row.job_id);
        if(result.status==='SUBMITTED')db.prepare('UPDATE submission_ledger SET confirmed_at=?,confirmation=? WHERE job_id=?').run(new Date().toISOString(),result.confirmation??null,row.job_id);
        // A later attempt can resolve a question a fix made moot (a classification fix, a saved answer): anything not raised again this
        // time is stale, not still open, so it must not sit in `questions` (or the digest) forever as if it still blocked the job.
        const stillOpen=new Set(result.questions.map(q=>q.question));
        for(const open of db.prepare("SELECT id,question FROM questions WHERE job_id=? AND status='OPEN'").all(row.job_id) as Array<{id:number;question:string}>)
          if(!stillOpen.has(open.question))db.prepare("UPDATE questions SET status='STALE' WHERE id=?").run(open.id);
        for(const question of result.questions)addQuestion(db,row.job_id,question.question,question.fieldName,question.classification,result.finalUrl);
        if(ledger&&result.status!=='SUBMITTED')addQuestion(db,row.job_id,'Submission may have reached the employer. Verify confirmation before any further action.','submission_confirmation','SENSITIVE',result.finalUrl);
        db.prepare('UPDATE run_jobs SET forms_inspected=?,fields_filled=?,resume_uploaded=?,application_status=?,reason=? WHERE run_id=? AND job_id=?').run(result.formsInspected,result.fieldsFilled,result.resumeUploaded?1:0,result.status,result.reason??null,runId,row.job_id);
        if(['FAILED','RETRYABLE_TECHNICAL_FAILURE','VALIDATION_FAILURE','BLOCKED_SECURITY','LOGIN_REQUIRED'].includes(result.status))db.prepare('INSERT INTO errors(run_id,stage,job_id,error,screenshot,timestamp) VALUES(?,?,?,?,?,?)').run(runId,'APPLICATION',row.job_id,result.reason??result.status,result.screenshot??null,new Date().toISOString());
      })();
      await mkdir(`${root}/reports/evidence`,{recursive:true});await writeFile(`${root}/reports/evidence/${runId}-job-${row.job_id}.json`,JSON.stringify(result,null,2));
      await logEvent(root,{runId,stage:'APPLICATION_RESULT',jobId:row.job_id,status:result.status,ats:result.ats,fieldsFilled:result.fieldsFilled,resumeUploaded:result.resumeUploaded,reason:result.reason});
      await page?.close().catch(()=>undefined);
      if(config.application.delay_between_jobs_ms&&processed<Math.min(rows.length,limit))await new Promise(resolve=>setTimeout(resolve,config.application.delay_between_jobs_ms));
    }
  }
  const remaining=(db.prepare("SELECT COUNT(*) n FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.evidence_mode='live' AND a.status='READY_FOR_REVIEW'").get() as {n:number}).n;
  await logEvent(root,{runId,stage:'APPLICATION_QUEUE',processed,remaining,limit,limitReached:processed>=limit&&remaining>0});
  return {processed,remaining,jobIds};
}
class ManualApplyError extends Error{constructor(url:string){super(`Apply manually on LinkedIn (Easy Apply or no employer link found): ${url}`);}}
function requireSeparator():string{return process.platform==='win32'?'\\':'/';}
