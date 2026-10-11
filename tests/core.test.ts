import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createServer} from 'node:http';
import {chromium} from 'playwright';
import {loadConfig,applicationSchema,searchSchema,candidateSchema} from '../src/config.js';
import {openDatabase} from '../src/database.js';
import {persistJob,ageBucket,matchesSearch,type JobInput} from '../src/jobs.js';
import {importJobs} from '../src/discovery.js';
import {beginSubmission,claimApplication,recoverInterruptedApplications,transitionApplication} from '../src/state.js';
import {processEligibleJobs} from '../src/pipeline.js';
import {processApplicationQueue} from '../src/application-runner.js';
import {getRunReport} from '../src/report.js';

const config=await loadConfig(process.cwd());
const jd='QA Engineer with 5+ years experience. Required: manual testing, functional testing, Selenium, Java and API testing. Build regression testing and automation testing for our Hyderabad team.';
function job(overrides:Partial<JobInput>={}):JobInput{return {company:'Controlled Test Company',title:'QA Engineer',location:'Hyderabad, India',workMode:'On-site',jobUrl:'https://jobs.example.test/jobs/1',applicationUrl:'https://jobs.example.test/jobs/1',source:'test',postedAt:new Date(Date.now()-3600000).toISOString(),description:jd,externalId:'1',...overrides};}
async function database(){const root=await mkdtemp(join(tmpdir(),'job-link-test-'));const db=openDatabase(root);return {root,db,close:async()=>{db.close();await rm(root,{recursive:true,force:true});}};}
function run(db:ReturnType<typeof openDatabase>,id='run'){db.prepare("INSERT INTO runs(id,started_at,status,mode) VALUES(?,?,'STARTED','fixture')").run(id,new Date().toISOString());}

test('config rejects invalid bounds, contradictory thresholds and invented schema fields',()=>{
  assert.equal(applicationSchema.safeParse({...config.application,max_jobs_per_run:0}).success,false);
  assert.equal(applicationSchema.safeParse({...config.application,review_min_score:90,auto_apply_min_score:70}).success,false);
  assert.equal(searchSchema.safeParse({...config.search,max_age_hours:8,priority_window_hours:24}).success,false);
  assert.equal(candidateSchema.safeParse({...config.candidate,email:'invalid'}).success,false);
  assert.equal(candidateSchema.safeParse({...config.candidate,made_up_skill:'Rust'}).success,false);
});
test('age boundaries preserve <=8 priority, <=24 eligibility and reject missing/future timestamps',()=>{
  const now=Date.parse('2026-09-16T12:00:00Z');const at=(hours:number)=>new Date(now-hours*3600000).toISOString();
  assert.equal(ageBucket(at(8),config,now),'PRIORITY');assert.equal(ageBucket(at(8.01),config,now),'SECONDARY');assert.equal(ageBucket(at(24),config,now),'SECONDARY');assert.equal(ageBucket(at(24.01),config,now),'STALE');assert.equal(ageBucket('',config,now),'UNKNOWN');assert.equal(ageBucket(at(-1),config,now),'FUTURE');
});
test('search respects candidate fallback locations and geographically restricted remote roles',()=>{
  assert.equal(matchesSearch(job(),config).matches,true);
  assert.equal(matchesSearch(job({location:'Remote - United States',workMode:'Remote'}),config).matches,false);
  assert.equal(matchesSearch(job({location:'Remote - India',workMode:'Remote'}),config).matches,true);
  assert.equal(matchesSearch(job({title:'Mechanical Quality Engineer'}),config).matches,false);
});
test('canonical, external ID, ATS aliases and company/title/location dedupe preserve submitted state',async()=>{
  const env=await database();try{
    const first=persistJob(env.db,job(),config);
    assert.equal(persistJob(env.db,job({jobUrl:'https://jobs.example.test/jobs/1?utm_source=x#apply'}),config).id,first.id);
    assert.equal(persistJob(env.db,job({jobUrl:'https://other.example.test/role'}),config).id,first.id);
    const fallback=persistJob(env.db,job({jobUrl:'https://third.example.test/job',applicationUrl:undefined,externalId:undefined}),config);assert.equal(fallback.id,first.id);
    const gh=persistJob(env.db,job({jobUrl:'https://boards.greenhouse.io/acme/jobs/12345',applicationUrl:undefined,externalId:'12345',source:'gh'}),config);
    assert.equal(persistJob(env.db,job({jobUrl:'https://job-boards.greenhouse.io/acme/jobs/12345',applicationUrl:undefined,externalId:'other-source',source:'linkedin'}),config).id,gh.id);
    env.db.prepare("INSERT INTO applications(job_id,status,submitted_at) VALUES(?,'SUBMITTED',?)").run(first.id,new Date().toISOString());
    env.db.prepare("UPDATE jobs SET status='SUBMITTED' WHERE id=?").run(first.id);persistJob(env.db,job(),config);
    assert.equal((env.db.prepare('SELECT status FROM jobs WHERE id=?').get(first.id) as {status:string}).status,'SUBMITTED');assert.equal(claimApplication(env.db,first.id,'new-run',2),false);
  }finally{await env.close();}
});
test('stale and unknown records persist but never queue; mandatory gaps override lower score thresholds',async()=>{
  const env=await database();try{run(env.db);const result=importJobs(env.db,[job({postedAt:new Date(Date.now()-25*3600000).toISOString()}),job({externalId:'2',jobUrl:'https://jobs.example.test/jobs/2',applicationUrl:undefined,postedAt:''}),job({externalId:'3',jobUrl:'https://jobs.example.test/jobs/3',applicationUrl:undefined,description:jd+' Python is mandatory.'})],{...config,application:{...config.application,review_min_score:20}},'run','live');assert.equal(result.tooOld,1);assert.equal(result.unknownAge,1);assert.equal(result.eligible,0);assert.equal((env.db.prepare('SELECT COUNT(*) n FROM jobs').get() as {n:number}).n,3);}finally{await env.close();}
});
test('durable claim and submission intent prohibit double submission and recover ambiguous outcomes',async()=>{
  const env=await database();try{run(env.db);const id=persistJob(env.db,job(),config).id;env.db.prepare("INSERT INTO applications(job_id,status) VALUES(?,'READY_FOR_REVIEW')").run(id);assert.equal(claimApplication(env.db,id,'run',2),true);assert.equal(claimApplication(env.db,id,'run',2),false);beginSubmission(env.db,id,'run');assert.throws(()=>beginSubmission(env.db,id,'run'));assert.throws(()=>transitionApplication(env.db,id,'READY_FOR_REVIEW','unsafe'));const recovery=recoverInterruptedApplications(env.db);assert.equal(recovery.ambiguous,1);assert.equal(claimApplication(env.db,id,'new',5),false);assert.equal((env.db.prepare('SELECT COUNT(*) n FROM questions').get() as {n:number}).n,1);}finally{await env.close();}
});
test('interrupted pre-submit filling is requeued without losing attempt count',async()=>{
  const env=await database();try{run(env.db);const id=persistJob(env.db,job(),config).id;env.db.prepare("INSERT INTO applications(job_id,status) VALUES(?,'READY_FOR_REVIEW')").run(id);claimApplication(env.db,id,'run',2);assert.equal(recoverInterruptedApplications(env.db).requeued,1);assert.equal(claimApplication(env.db,id,'next',2),true);assert.equal((env.db.prepare('SELECT attempt_count n FROM applications').get() as {n:number}).n,2);}finally{await env.close();}
});
test('controlled multi-job browser integration processes batches and resumes remaining queue',async()=>{
  const env=await database();let browser:Awaited<ReturnType<typeof chromium.launch>>|undefined;
  const server=createServer((request,response)=>{const id=request.url?.split('/').at(-1);response.end(`<html><body><h1>QA Engineer</h1><h2>Controlled Test Company ${id}</h2><form><label>Full name<input name="full_name" required></label><label>Email<input name="email" type="email" required></label><label>Resume<input name="resume" type="file" required></label><button type="submit">Submit application</button></form></body></html>`);});
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));const address=server.address() as {port:number};const base=`http://127.0.0.1:${address.port}`;
  try{await mkdir(join(env.root,'resumes'));await writeFile(join(env.root,'resumes','test.pdf'),'%PDF-1.4 controlled upload regression');env.db.prepare("INSERT INTO resumes(filename,category,keywords,extracted_keywords,updated_at,available) VALUES('test.pdf','GENERAL_QA','[\"Java\",\"Selenium\"]','[]',?,1)").run(new Date().toISOString());
    run(env.db,'first');const feed=Array.from({length:5},(_,i)=>job({company:`Controlled Test Company ${i}`,externalId:String(i),jobUrl:`${base}/jobs/${i}`,applicationUrl:`${base}/jobs/${i}`,postedAt:new Date(Date.now()-(i===4?1:12)*3600000).toISOString()}));
    importJobs(env.db,feed,config,'first','live');assert.equal((await processEligibleJobs(env.root,env.db,config,'first')).queued,5);
    browser=await chromium.launch({headless:true});const context=await browser.newContext();const bounded={...config,application:{...config.application,max_jobs_per_run:3,batch_size:2,delay_between_jobs_ms:0}};
    const result=await processApplicationQueue(env.root,env.db,bounded,'first',async()=>context);assert.equal(result.processed,3);assert.equal(result.remaining,2);
    assert.equal((env.db.prepare("SELECT j.external_id FROM application_events e JOIN jobs j ON j.id=e.job_id WHERE stage='CLAIMED' ORDER BY e.id LIMIT 1").get() as {external_id:string}).external_id,'4');
    assert.equal(getRunReport(env.db,'first').metrics.formsPopulated,3);assert.equal(getRunReport(env.db,'first').metrics.resumeUploads,3);
    run(env.db,'second');const second=await processApplicationQueue(env.root,env.db,bounded,'second',async()=>context);assert.equal(second.processed,2);assert.equal(second.remaining,0);
    run(env.db,'third');assert.equal((await processApplicationQueue(env.root,env.db,bounded,'third',async()=>context)).processed,0);
    assert.equal((env.db.prepare("SELECT COUNT(*) n FROM applications WHERE status='READY_FOR_SUBMISSION'").get() as {n:number}).n,5);
  }finally{await browser?.close();await new Promise<void>(resolve=>server.close(()=>resolve()));await env.close();}
});
