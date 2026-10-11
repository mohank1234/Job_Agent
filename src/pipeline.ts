import type Database from 'better-sqlite3';
import type { Config } from './config.js';
import { selectResume } from './resumes.js';
import { ageBucket, levelExclusion } from './jobs.js';
import { duplicateApplicationOf } from './exclusions.js';
import { addQuestion } from './state.js';
import { logEvent } from './logger.js';
import { compareByPriority } from './priority.js';
import { applyKindOf, isLinkedInUrl } from './sources/normalize.js';

export type PipelineResult={considered:number;queued:number;needsUserInput:number;skipped:number;remaining:number};
export async function processEligibleJobs(root:string,db:Database.Database,config:Config,runId:string):Promise<PipelineResult> {
  const jobs=db.prepare(`SELECT j.* FROM jobs j WHERE j.status='REVIEW' AND j.evidence_mode='live'
    AND NOT EXISTS(SELECT 1 FROM applications a WHERE a.job_id=j.id)
    `).all() as Array<{id:number;company:string;title:string;description:string;location:string|null;work_mode:string|null;fit_score:number;application_url:string|null;posted_at:string;detected_at:string|null;metadata:string|null}>;
  jobs.sort(compareByPriority(config.search.location_priority));
  let queued=0,needsUserInput=0,skipped=0;
  db.transaction(()=>{
    for(const job of jobs) {
      const age=ageBucket(job.posted_at,config);
      if(age==='STALE'||age==='FUTURE'||(age==='UNKNOWN'&&!config.search.allow_unknown_posted_at)) {db.prepare("UPDATE jobs SET status='SKIPPED',filter_reason=? WHERE id=?").run(`Expired before application queue: ${age}`,job.id);skipped++;continue;}
      // Rules can change after a job was scored (a new minimum experience); and a repost of a role already applied to is not a second job.
      const belowLevel=levelExclusion(job,config);
      if(belowLevel){db.prepare("UPDATE jobs SET status='SKIPPED',filter_reason=? WHERE id=?").run(belowLevel,job.id);skipped++;continue;}
      const original=duplicateApplicationOf(db,job);
      if(original){db.prepare("UPDATE jobs SET status='DUPLICATE',filter_reason=? WHERE id=?").run(`Duplicate of job ${original}: the same role at the same company`,job.id);skipped++;continue;}
      if(!job.application_url) {db.prepare("UPDATE jobs SET status='NEEDS_USER_INPUT' WHERE id=?").run(job.id);addQuestion(db,job.id,'No application URL was available for this job.');needsUserInput++;continue;}
      const resume=selectResume(db,job.title,job.description);
      if(!resume){db.prepare("UPDATE jobs SET status='NEEDS_USER_INPUT' WHERE id=?").run(job.id);addQuestion(db,job.id,'No available readable resume is registered for this job.');needsUserInput++;continue;}
      // Easy Apply forms are filled by the LinkedIn adapter like any other queued application, and so is a LinkedIn job whose apply kind
      // discovery could not read (its button had not rendered): the application run opens it once and the page decides. Only a posting
      // known to apply elsewhere with no employer link found is a manual-apply task.
      if(isLinkedInUrl(job.application_url)&&applyKindOf(job.metadata)==='external'){
        const message=`Apply manually on LinkedIn (Easy Apply or no employer link found) with resume "${resume.filename}": ${job.application_url}`;
        db.prepare(`INSERT INTO applications(job_id,application_url,resume,resume_id,resume_reason,started_at,status,ats,last_stage,last_error,updated_at) VALUES(?,?,?,?,?,?,'NEEDS_USER_INPUT','linkedin','MANUAL_APPLY',?,?)`).run(job.id,job.application_url,resume.filename,resume.id??null,resume.selectionReason??'Selected by resume registry',new Date().toISOString(),message,new Date().toISOString());
        db.prepare("UPDATE jobs SET status='NEEDS_USER_INPUT' WHERE id=?").run(job.id);
        db.prepare(`INSERT INTO run_jobs(run_id,job_id,queued,application_status,reason) VALUES(?,?,0,'NEEDS_USER_INPUT',?) ON CONFLICT(run_id,job_id) DO UPDATE SET application_status='NEEDS_USER_INPUT',reason=excluded.reason`).run(runId,job.id,message);
        addQuestion(db,job.id,message,'manual_apply','MANUAL_APPLY',job.application_url);needsUserInput++;continue;
      }
      db.prepare(`INSERT INTO applications(job_id,application_url,resume,resume_id,resume_reason,started_at,status,updated_at) VALUES(?,?,?,?,?,?,'READY_FOR_REVIEW',?)`).run(job.id,job.application_url,resume.filename,resume.id??null,resume.selectionReason??'Selected by resume registry',new Date().toISOString(),new Date().toISOString());
      db.prepare("UPDATE jobs SET status='APPLICATION_READY' WHERE id=?").run(job.id);
      db.prepare(`INSERT INTO run_jobs(run_id,job_id,queued) VALUES(?,?,1) ON CONFLICT(run_id,job_id) DO UPDATE SET queued=1`).run(runId,job.id);
      queued++;
    }
  })();
  const remaining=Number((db.prepare("SELECT COUNT(*) n FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.status='READY_FOR_REVIEW' AND j.evidence_mode='live'").get() as {n:number}).n);
  const result={considered:jobs.length,queued,needsUserInput,skipped,remaining};
  await logEvent(root,{runId,stage:'QUEUE',action:'processed',...result});return result;
}
