import type Database from 'better-sqlite3';
import type { Config } from './config.js';
import { persistJob, type JobInput } from './jobs.js';

export type DiscoveryResult = {discovered:number;accepted:number;tooOld:number;duplicates:number;fresh8:number;fresh24:number;unknownAge:number;future:number;rejectedSearch:number;scored:number;highFit:number;mediumFit:number;lowFit:number;eligible:number;invalid:number;ids:number[]};
export function importJobs(db:Database.Database,jobs:JobInput[],config:Config,runId?:string,mode:'live'|'fixture'='fixture'):DiscoveryResult {
  const result:DiscoveryResult={discovered:jobs.length,accepted:0,tooOld:0,duplicates:0,fresh8:0,fresh24:0,unknownAge:0,future:0,rejectedSearch:0,scored:0,highFit:0,mediumFit:0,lowFit:0,eligible:0,invalid:0,ids:[]};
  const seen=new Set<number>();
  const now=Date.now();
  for(const job of jobs) {
    try {
      const item=persistJob(db,job,config,mode,now);
      if(item.duplicate) result.duplicates++;
      if(seen.has(item.id)) continue;
      seen.add(item.id);result.ids.push(item.id);
      if(item.bucket==='PRIORITY') result.fresh8++;
      if(item.bucket==='SECONDARY') result.fresh24++;
      if(item.bucket==='STALE') result.tooOld++;
      if(item.bucket==='UNKNOWN') result.unknownAge++;
      if(item.bucket==='FUTURE') result.future++;
      if(item.disposition==='REJECTED_SEARCH') result.rejectedSearch++;
      if(item.score!==null) {result.scored++;if(item.score>=config.application.auto_apply_min_score)result.highFit++;else if(item.score>=config.application.review_min_score)result.mediumFit++;else result.lowFit++;}
      if(item.disposition==='ELIGIBLE')result.eligible++;
      if(!['STALE','FUTURE','UNKNOWN'].includes(item.disposition))result.accepted++;
      if(runId)db.prepare(`INSERT INTO run_jobs(run_id,job_id,disposition,age_bucket,score,reason) VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,job_id) DO NOTHING`).run(runId,item.id,item.disposition,item.bucket,item.score,item.reason);
    } catch(error) {
      result.invalid++;
      if(runId)db.prepare('INSERT INTO errors(run_id,stage,error,timestamp) VALUES(?,?,?,?)').run(runId,'NORMALIZATION',String(error),new Date().toISOString());
    }
  }
  return result;
}
