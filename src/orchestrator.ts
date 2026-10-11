import { randomUUID } from 'node:crypto';
import { open, readFile, unlink, mkdir } from 'node:fs/promises';
import type { BrowserContext } from 'playwright';
import type Database from 'better-sqlite3';
import { logEvent } from './logger.js';

export async function acquireRunLock(root:string):Promise<()=>Promise<void>> {
  await mkdir(`${root}/data`,{recursive:true});
  const file=`${root}/data/run.lock`;
  for(let attempt=0;attempt<2;attempt++) {
    try {const handle=await open(file,'wx');await handle.writeFile(JSON.stringify({pid:process.pid,startedAt:new Date().toISOString()}));await handle.close();return async()=>{await unlink(file).catch(()=>undefined);};}
    catch(error) {
      if((error as NodeJS.ErrnoException).code!=='EEXIST')throw error;
      const lock=JSON.parse(await readFile(file,'utf8')) as {pid:number};
      let live=false;try {process.kill(lock.pid,0);live=true;}catch(error){if((error as NodeJS.ErrnoException).code!=='ESRCH')live=true;}
      if(live)throw new Error(`Another job-link process is active (PID ${lock.pid}). Its application queue and browser profile are locked.`);
      await unlink(file);
    }
  }
  throw new Error('Could not acquire run lock');
}
export async function startRun(root:string,db:Database.Database,mode='live'):Promise<{runId:string;finish:(status:string,metrics?:Record<string,unknown>)=>Promise<void>}> {
  const runId=randomUUID();
  db.prepare('INSERT INTO runs(id,started_at,status,mode) VALUES(?,?,?,?)').run(runId,new Date().toISOString(),'STARTED',mode);
  await logEvent(root,{runId,stage:'RUN',action:'started',mode});
  return {runId,finish:async(status,metrics={})=>{db.prepare('UPDATE runs SET finished_at=?,status=?,metrics_json=? WHERE id=?').run(new Date().toISOString(),status,JSON.stringify(metrics),runId);await logEvent(root,{runId,stage:'RUN',action:'finished',status});}};
}
export async function waitForManualLogin(context:BrowserContext,urls:string[]):Promise<void> {
  if(!process.stdin.isTTY)throw new Error('Manual login needs an interactive terminal: npm run dev -- login');
  for(const url of urls) {const page=await context.newPage();await page.goto(url,{waitUntil:'domcontentloaded'});}
  console.log('Complete legitimate login in the open browser tabs. Press Enter in this terminal when finished.');
  await new Promise<void>(resolve=>process.stdin.once('data',()=>resolve()));
}
/** One keypress per invitation: y sends it, n leaves it for later, s drops that person, a approves everything still queued, q stops. The note is always printed first. */
export function makeSendConfirmer():(prompt:string)=>Promise<boolean|'skip'|'stop'> {
  let approveAll=false;
  return async(prompt)=>{
    if(approveAll){process.stdout.write(`${prompt.split('\n[y]')[0]}\n  (approved with "a")\n`);return true;}
    if(!process.stdin.isTTY)throw new Error('Sending LinkedIn invitations needs an interactive terminal to approve them.');
    process.stdout.write(prompt);
    const answer=await new Promise<string>(resolve=>process.stdin.once('data',data=>resolve(data.toString().trim().toLowerCase())));
    if(answer==='a'||answer==='all'){approveAll=true;return true;}
    if(answer==='y'||answer==='yes')return true;
    if(answer==='s'||answer==='skip')return 'skip';
    if(answer==='q'||answer==='quit')return 'stop';
    return false;
  };
}
export async function confirmAction(prompt:string):Promise<boolean> {
  if(!process.stdin.isTTY)throw new Error('This action needs an interactive terminal to confirm.');
  process.stdout.write(prompt);
  return new Promise<boolean>(resolve=>process.stdin.once('data',data=>resolve(/^y(es)?$/i.test(data.toString().trim()))));
}
