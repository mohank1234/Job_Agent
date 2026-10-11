import { isClosedJob } from './ats/job-state.js';
import { createHash } from 'node:crypto';
import type Database from 'better-sqlite3';
import type { Config } from './config.js';
import type { Job } from './core/types.js';
import { requiredExperienceYears, scoreJob } from './fit.js';
import { canonicalUrl, isLinkedInUrl, normalizeJob } from './sources/normalize.js';

export type JobInput = Job;
export type AgeBucket = 'PRIORITY' | 'SECONDARY' | 'OLDER_ALLOWED' | 'STALE' | 'UNKNOWN' | 'FUTURE';
export function ageBucket(postedAt: string, config: Config, now = Date.now()): AgeBucket {
  const age = (now - Date.parse(postedAt)) / 3600000;
  if (!Number.isFinite(age)) return 'UNKNOWN';
  if (age < 0) return 'FUTURE';
  if (age > config.search.max_age_hours) return 'STALE';
  if (age <= config.search.priority_window_hours) return 'PRIORITY';
  if (age <= 24) return 'SECONDARY';
  return 'OLDER_ALLOWED';
}
const normalizedText = (value: string): string => value.normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
export function jobIdentities(job: JobInput): string[] {
  const identities = [`url:${canonicalUrl(job.jobUrl)}`];
  if (job.applicationUrl) identities.push(`url:${canonicalUrl(job.applicationUrl)}`);
  if (job.externalId) identities.push(`external:${job.source.toLowerCase()}:${job.externalId}`);
  for (const raw of [job.jobUrl, job.applicationUrl].filter(Boolean) as string[]) {
    const url = new URL(raw);
    if (isLinkedInUrl(raw)) { const id=url.pathname.match(/\/jobs\/view\/(\d+)/)?.[1]; if(id)identities.push(`linkedin:${id}`); }
    const greenhouse = url.hostname.match(/(?:boards(?:-api)?\.(?:eu\.)?greenhouse\.io|job-boards\.(?:eu\.)?greenhouse\.io)/);
    const ghId = url.searchParams.get('gh_jid') ?? (greenhouse ? url.pathname.match(/(?:jobs\/)?(\d+)(?:\/|$)/)?.[1] : undefined);
    if (ghId) identities.push(`ats:greenhouse:${ghId}`);
    if (/lever\.co$/.test(url.hostname)) { const parts = url.pathname.split('/').filter(Boolean); if (parts[1]) identities.push(`ats:lever:${parts[0]}:${parts[1]}`); }
    if (/ashbyhq\.com$/.test(url.hostname)) { const parts = url.pathname.split('/').filter(Boolean); if (parts[1]) identities.push(`ats:ashby:${parts[0]}:${parts[1]}`); }
    if (job.atsJobId) identities.push(`ats:${url.hostname}:${job.atsJobId}`);
  }
  return [...new Set(identities)];
}
export function identityKey(job: JobInput): string {
  return createHash('sha256').update([job.company,job.title,job.location].map(normalizedText).join('|')).digest('hex');
}

// Title words for roles below the experience the candidate targets (matched on the normalized title: "Jr." is "jr").
const EARLY_CAREER = /\b(?:intern|interns|internship|internships|trainee|trainees|apprentice|apprenticeship|fresher|freshers|junior|jr|entry level|graduate)\b/;
/** Why a posting is below the level the candidate applies to (an internship, or fewer years than `min_job_experience_years`), or undefined. */
export function levelExclusion(job: Pick<JobInput, 'title' | 'description'>, config: Config): string | undefined {
  if (config.search.exclude_internships && (EARLY_CAREER.test(normalizedText(job.title)) || /\bunpaid\b|\binternship\b/i.test(job.description ?? '')))
    return 'Internship or entry-level role';
  const minimum = config.search.min_job_experience_years;
  const level = minimum ? requiredExperienceYears(job.title, job.description ?? '') : undefined;
  return level !== undefined && level < minimum ? `Posting asks for ${level} years of experience, below your minimum of ${minimum}` : undefined;
}
export function matchesSearch(job: JobInput, config: Config): {matches: boolean; reason?: string} {
  const title = normalizedText(job.title);
  const configured = config.search.role_keywords.some(role => title.includes(normalizedText(role)));
  const qaRelated = /\b(qa|sdet|tester|quality assurance|quality engineer(?:ing)?|quality analyst|test engineer|test analyst|test lead|qa lead|qa analyst|engineer in test|software test|test automation(?: engineer)?|automation test(?:ing|er)?|llm test|rag test|genai test|prompt test|ai agent test|ai ml test|ml ai test|ai test|ml test|machine learning test)\b/.test(title);
  if (!configured && !qaRelated) return {matches:false,reason:'Role does not match configured QA/testing search'};
  if (/\b(manufacturing|mechanical|hardware|food|pharma|civil)\b/.test(title) && !/software/.test(title)) return {matches:false,reason:'Non-software quality role'};
  const belowLevel = levelExclusion(job, config);
  if (belowLevel) return {matches:false,reason:belowLevel};
  const locations = config.search.locations.length ? config.search.locations : config.candidate.preferred_locations;
  if (!locations.length) return {matches:true};
  const location = normalizedText(job.location);
  const remote = /remote/i.test(`${job.workMode} ${job.location}`);
  const local = locations.filter(item=> !/^remote$/i.test(item)).some(item=>location.includes(normalizedText(item)));
  if (local) return {matches:true};
  if (remote && locations.some(item=>/^remote$/i.test(item))) {
    const homeCountry = config.candidate.country ?? config.candidate.location.split(',').at(-1)?.trim();
    if (/worldwide|anywhere|global/.test(location) || /^(remote|remote work|anywhere)$/.test(location)) return {matches:true};
    if (homeCountry && location.includes(normalizedText(homeCountry))) return {matches:true};
    return {matches:false,reason:`Remote role has a geographic restriction not verified compatible with ${config.candidate.location}`};
  }
  return {matches:false,reason:'Location is outside configured preferences'};
}

/** The same posting is found by several searches, and copies differ in what they carry (timestamp, description, employer link).
 * Evaluate the union of what is known so a poorer copy can never overwrite a fuller evaluation. */
function mergeWithStored(db: Database.Database, job: Job, id: number): Job {
  const stored = db.prepare('SELECT posted_at,description,application_url,metadata FROM jobs WHERE id=?').get(id) as {posted_at: string|null; description: string|null; application_url: string|null; metadata: string|null} | undefined;
  if (!stored) return job;
  let storedMetadata: Record<string, unknown> = {};
  try { storedMetadata = JSON.parse(stored.metadata ?? '{}') as Record<string, unknown>; } catch { /* unreadable metadata is ignored */ }
  const keepStoredUrl = !!stored.application_url && !isLinkedInUrl(stored.application_url) && (!job.applicationUrl || isLinkedInUrl(job.applicationUrl));
  return { ...job, postedAt: job.postedAt || stored.posted_at || '', description: (stored.description ?? '').length > job.description.length ? stored.description! : job.description,
    applicationUrl: keepStoredUrl ? stored.application_url! : job.applicationUrl, metadata: { ...storedMetadata, ...job.metadata } };
}

export type PersistResult = {id:number;duplicate:boolean;job:Job;bucket:AgeBucket;disposition:string;score:number|null;reason:string};
export function persistJob(db: Database.Database, input: JobInput, config: Config, mode: 'live'|'fixture' = 'live', now = Date.now()): PersistResult {
  let job = normalizeJob(input);
  let identities = jobIdentities(job);
  let key = identityKey(job);
  return db.transaction(() => {
    let existing: {id:number;status:string;evidence_mode:string} | undefined;
    for (const identity of identities) {
      existing = db.prepare('SELECT j.id,j.status,j.evidence_mode FROM jobs j JOIN job_identities i ON i.job_id=j.id WHERE i.identity=?').get(identity) as typeof existing;
      if (existing) break;
    }
    if (!existing) existing = db.prepare('SELECT id,status,evidence_mode FROM jobs WHERE canonical_url=? OR job_url=?').get(job.jobUrl,job.jobUrl) as typeof existing;
    // Same role/company/location within a posting window is conservatively one application.
    // Distinct explicit requisition IDs are kept distinct.
    if (!existing) existing = db.prepare(`SELECT id,status,evidence_mode FROM jobs WHERE identity_key=? AND evidence_mode=?
      AND status!='CLOSED_JOB' AND (external_id IS NULL OR ? IS NULL) AND ABS(julianday(COALESCE(posted_at,detected_at))-julianday(?))<=7 LIMIT 1`).get(key,mode,job.externalId??null,job.postedAt||job.discoveredAt) as typeof existing;
    if (existing) { job = mergeWithStored(db, job, existing.id); identities = jobIdentities(job); key = identityKey(job); }
    const bucket = ageBucket(job.postedAt,config,now);
    const filter = matchesSearch(job,config);
    let disposition = 'REJECTED_SEARCH', reason = filter.reason ?? '', score: number|null = null;
    let fit: ReturnType<typeof scoreJob> | undefined;
    if (existing?.status === 'CLOSED_JOB' || isClosedJob(input.description)) {
      disposition = 'CLOSED_JOB'; reason = 'Posting explicitly closed or inactive';
    } else if (bucket==='STALE'||bucket==='FUTURE'||(bucket==='UNKNOWN'&&!config.search.allow_unknown_posted_at)) {
      disposition = bucket; reason = bucket==='UNKNOWN'?'Source has no verifiable posting timestamp':`Posting age is ${bucket.toLowerCase()}`;
    } else if (filter.matches) {
      fit = scoreJob(job.description,config,{title:job.title,location:job.location,workMode:job.workMode});
      score=fit.score; disposition=!fit.mandatoryMissing.length&&score>=config.application.review_min_score?'ELIGIBLE':'LOW_FIT';reason=fit.reason;
    }
    const hasApplication = existing ? db.prepare('SELECT id FROM applications WHERE job_id=?').get(existing.id) : undefined;
    const timestamp = new Date(now).toISOString();
    const values = {company:job.company,title:job.title,location:job.location,workMode:job.workMode,jobUrl:job.jobUrl,source:job.source,postedAt:job.postedAt||null,detectedAt:job.discoveredAt??timestamp,description:job.description,applicationUrl:job.applicationUrl??null,externalId:job.externalId??null,atsJobId:job.atsJobId??null,identityKey:key,metadata:JSON.stringify(job.metadata??{}),mode,score,fitReason:fit?.reason??null,fitDetails:fit?JSON.stringify(fit):null,status:disposition==='CLOSED_JOB'?'CLOSED_JOB':disposition==='ELIGIBLE'?'REVIEW':'SKIPPED',filterReason:reason,updatedAt:timestamp};
    let id: number;
    if (existing) {
      id=existing.id;
      // An observed duplicate cannot reset a completed, blocked, or previously attempted application.
      if (!hasApplication) db.prepare(`UPDATE jobs SET description=@description,posted_at=COALESCE(@postedAt,posted_at),fit_score=@score,fit_reason=@fitReason,fit_details=@fitDetails,status=@status,filter_reason=@filterReason,updated_at=@updatedAt,canonical_url=@jobUrl,identity_key=@identityKey,metadata=@metadata WHERE id=@id`).run({...values,id});
    } else {
      id=Number(db.prepare(`INSERT INTO jobs(company,title,location,work_mode,job_url,canonical_url,source,posted_at,detected_at,description,application_url,external_id,ats_job_id,identity_key,metadata,evidence_mode,fit_score,fit_reason,fit_details,status,filter_reason,updated_at)
        VALUES(@company,@title,@location,@workMode,@jobUrl,@jobUrl,@source,@postedAt,@detectedAt,@description,@applicationUrl,@externalId,@atsJobId,@identityKey,@metadata,@mode,@score,@fitReason,@fitDetails,@status,@filterReason,@updatedAt)`).run(values).lastInsertRowid);
    }
    if (disposition === 'CLOSED_JOB') {
      db.prepare("UPDATE jobs SET status='CLOSED_JOB',filter_reason=? WHERE id=?").run(reason,id);
      db.prepare("UPDATE applications SET status='CLOSED_JOB',retryable=0,last_error=? WHERE job_id=? AND submitted_at IS NULL AND NOT EXISTS(SELECT 1 FROM submission_ledger WHERE job_id=?)").run(reason,id,id);
      db.prepare("UPDATE questions SET status='STALE' WHERE job_id=? AND status='OPEN'").run(id);
    }
    for (const identity of identities) db.prepare('INSERT OR IGNORE INTO job_identities(identity,job_id) VALUES(?,?)').run(identity,id);
    return {id,duplicate:!!existing,job,bucket,disposition,score,reason};
  })();
}
export function upsertJob(db: Database.Database, job: JobInput, config: Config): number { return persistJob(db,job,config).id; }
