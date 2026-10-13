import Database from 'better-sqlite3';
import type { Contact, OutreachDraft, OutreachType } from './core/types.js';
import { audienceOf, buildConnectionNote, buildExactConnectionNote, buildInMail, validateMessage } from './outreach.js';
import { canonicalContactUrl, companyKey, discoverContacts } from './contact-sources.js';
import type { Config } from './config.js';
import { logEvent } from './logger.js';
import { applyLedger, readLedger } from './outreach-ledger.js';

// Hiring people and managers first; company leadership is worth writing to too, but last.
const roleWeights: Array<[RegExp, number]> = [
  [/hiring manager/i, 95], [/(?:technical|engineering|it) recruiter/i, 95],
  [/recruit|talent acquisition|talent partner|talent manager|head of talent/i, 90],
  [/(?:qa|quality|test(?:ing)?|sdet)\b[^,|]*\b(?:manager|lead|head|director)|(?:manager|lead|head|director)\b[^,|]*\b(?:qa|quality|test(?:ing)?)/i, 90],
  [/human resources|\bhr\b|people (?:operations|partner)/i, 85],
  [/engineering manager|software (?:development )?manager|director[^,|]*engineering|head of engineering|vp[^,|]*engineering/i, 85],
  [/\bcto\b|\bceo\b|chief|founder|co-?founder|president|managing director/i, 65]
];
export function rankContacts(contacts: Contact[]): Contact[] {
  return contacts.map((contact) => ({ ...contact, relevanceScore: Math.max(contact.relevanceScore, roleWeights.find(([pattern]) => pattern.test(contact.title))?.[1] ?? 0) })).sort((left, right) => right.relevanceScore - left.relevanceScore || left.name.localeCompare(right.name));
}

/** Up to `limit` people for one company: one recruiter/HR contact and one manager first, then the next best of either, and a CEO-level contact only if a place is left. */
export function pickBalanced(contacts: Contact[], limit: number): Contact[] {
  const ranked = rankContacts(contacts);
  const staff = ranked.filter((contact) => audienceOf(contact.title) !== 'EXEC');
  const picked: Contact[] = [];
  const take = (contact?: Contact): void => { if (contact && !picked.includes(contact) && picked.length < limit) picked.push(contact); };
  take(staff.find((contact) => audienceOf(contact.title) === 'HR'));
  take(staff.find((contact) => audienceOf(contact.title) === 'MANAGER'));
  for (const contact of staff) take(contact);
  take(ranked.find((contact) => audienceOf(contact.title) === 'EXEC'));
  return picked;
}

function listAnd(items: string[]): string { return items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`; }
/** The strengths worth leading with for this kind of job, taken only from the candidate profile. */
export function focusSkills(jobTitle: string, candidate: Config['candidate']): string {
  const automation = candidate.automation_skills; const ai = candidate.ai_testing_skills; const skills = candidate.skills;
  if (/\b(?:ai|llm|rag|genai|gen ai|ml|machine learning|agents?)\b/i.test(jobTitle) && ai.length) return listAnd(ai.slice(0, 3));
  if (/sdet|automation|framework|playwright|selenium|developer in test|engineer in test/i.test(jobTitle) && automation.length) return `${listAnd(automation.slice(0, 3))} automation`;
  if (/manual|functional|tester|analyst/i.test(jobTitle)) {
    const manual = skills.filter((skill) => /manual testing|functional testing|regression testing|integration testing|system testing|api testing/i.test(skill)).slice(0, 3);
    if (manual.length) return listAnd(manual);
  }
  return automation.length ? `${listAnd(automation.slice(0, 2))} automation` : listAnd(skills.slice(0, 2));
}

const skillAliases: Record<string, RegExp> = {
  'API Testing': /\bapi (?:test|automation|validation)|\brest(?:ful)? apis?\b/i, 'Automation Testing': /test automation|automation (?:test|framework)|automated test/i,
  'Manual Testing': /manual test/i, 'Regression Testing': /regression/i, 'UI Automation': /ui automation|ui test/i, 'Functional Testing': /functional test/i
};
/** The candidate's own skills that this posting actually asks for, most-mentioned first; never a skill the profile lacks. */
export function jdRelevantSkills(title: string, description: string, candidate: Config['candidate']): string[] {
  const text = `${title}
${description}`;
  const pool = [...new Set([...candidate.automation_skills, ...candidate.ai_testing_skills, ...candidate.skills])];
  const escape = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const scored = pool.map((skill, order) => {
    const pattern = skillAliases[skill] ?? new RegExp(`(?<![\w])${escape(skill)}(?![\w])`, 'i');
    const hits = text.match(new RegExp(pattern.source, 'gi'))?.length ?? 0;
    return { skill, hits, order };
  }).filter((item) => item.hits > 0 && !/^quality assurance$/i.test(item.skill));
  return scored.sort((a, b) => b.hits - a.hits || a.order - b.order).slice(0, 6).map((item) => item.skill);
}

function canPrepare(db: Database.Database, config: Config, jobId: number, contact: Contact): boolean {
  const existing = db.prepare('SELECT o.job_id, o.created_at, o.sent_at, c.company FROM outreach o JOIN contacts c ON c.id=o.contact_id').all() as Array<{ job_id: number; created_at: string | null; sent_at: string | null; company: string }>;
  const cooldown = Date.now() - config.outreach.same_company_cooldown_days * 86_400_000;
  const profileExisting = db.prepare('SELECT o.job_id FROM outreach o JOIN contacts c ON c.id=o.contact_id WHERE c.linkedin_url = ?').all(canonicalContactUrl(contact.profileUrl)) as Array<{job_id: number}>;
  if (profileExisting.length) return false;
  return !existing.some((row) => companyKey(row.company) === companyKey(contact.company) && row.job_id !== jobId && (Date.parse(row.sent_at ?? row.created_at ?? '') || Date.now()) > cooldown);
}
/** People at this company already written to or waiting to be (anyone the user said never to contact does not count against the limit). */
function companyOutreachCount(db: Database.Database, company: string): number {
  const rows = db.prepare('SELECT c.company, o.status FROM outreach o JOIN contacts c ON c.id=o.contact_id').all() as Array<{ company: string; status: string }>;
  return rows.filter((row) => companyKey(row.company) === companyKey(company) && row.status !== 'DECLINED').length;
}
const hasOutreach = (db: Database.Database, profileUrl: string): boolean => !!db.prepare('SELECT 1 FROM outreach o JOIN contacts c ON c.id=o.contact_id WHERE c.linkedin_url = ?').get(canonicalContactUrl(profileUrl));

/** What the note is signed with: an explicit `signature`, else the candidate's name without a trailing middle initial ("Asha Rao K" signs as "Asha Rao"). */
export function outreachSignature(config: Config): string {
  if (config.outreach.signature) return config.outreach.signature;
  const parts = config.candidate.full_name.trim().split(/\s+/);
  return (parts.length > 2 && parts[parts.length - 1].replace(/\./g, '').length === 1 ? parts.slice(0, -1) : parts).join(' ');
}

export function draftOutreach(db: Database.Database, config: Config, jobId: number, contact: Contact, job: { company: string; title: string; description?: string }, type: OutreachType): OutreachDraft | undefined {
  if (!canPrepare(db, config, jobId, contact)) return undefined;
  const submitted = Boolean(db.prepare("SELECT 1 FROM applications WHERE job_id=? AND status='SUBMITTED' AND submitted_at IS NOT NULL").get(jobId));
  const input = { firstName: contact.name.split(/\s+/)[0], company: job.company, title: job.title, candidateName: config.candidate.full_name, specialization: focusSkills(job.title, config.candidate), submitted, years: config.candidate.years_experience, audience: audienceOf(contact.title) };
  const limit = Math.min(300, config.outreach.connection_note_limit);
  // Exactly 300 characters (signature included) with only the skills this posting asks for; a shorter configured limit keeps the older flexible note.
  const exact = config.outreach.exact_note_length && limit === 300;
  const skills = jdRelevantSkills(job.title, job.description ?? '', config.candidate);
  const message = type !== 'CONNECTION' ? buildInMail(input)
    : exact ? buildExactConnectionNote({ firstName: input.firstName, company: job.company, title: job.title, signature: outreachSignature(config), skills: skills.length ? skills : focusSkills(job.title, config.candidate).replace(/ automation$/, '').split(/, | and /).filter(Boolean), years: input.years, audience: input.audience, submitted })
    : buildConnectionNote(input, limit);
  validateMessage(message, type === 'CONNECTION' ? config.outreach.connection_note_limit : 4000);
  return { type, recipient: contact, message, characterCount: [...message].length };
}
export type OutreachResult = { contactsDiscovered: number; prepared: number; completed: number; duplicates: number; sourcesAttempted: number; sourcesWorking: number; errors: string[] };
export async function prepareOutreachForJobs(root: string, db: Database.Database, config: Config, runId: string, jobIds: number[]): Promise<OutreachResult> {
  const jobs = jobIds.map((id) => db.prepare('SELECT id, company, title, description, fit_score, metadata FROM jobs WHERE id=?').get(id) as { id: number; company: string; title: string; description: string; fit_score: number; metadata: string | null } | undefined)
    .filter((job): job is NonNullable<typeof job> => !!job && (job.fit_score >= config.application.review_min_score || /"sourceName":"LinkedIn alert email"/.test(job.metadata ?? '')));
  // Invitations already sent (or declined) must never be drafted again, even if the database was restored from an older copy.
  applyLedger(db, await readLedger(root));
  const discovery = await discoverContacts(root, [...new Set(jobs.map((job) => job.company))]);
  // The person LinkedIn shows as "Job poster" (and the rest of "Meet the hiring team") is the best person to write to for that job.
  const hiringTeam = (job: { metadata: string | null }): Array<{ name: string; title: string; profileUrl: string }> => { try { return (JSON.parse(job.metadata ?? '{}') as { hiringTeam?: Array<{ name: string; title: string; profileUrl: string }> }).hiringTeam ?? []; } catch { return []; } };
  const posters = new Map<number, Contact[]>(jobs.map((job) => [job.id, hiringTeam(job).slice(0, 2).map((member, index) => ({ company: job.company, name: member.name, title: member.title || 'Hiring team', profileUrl: member.profileUrl, relevanceScore: index === 0 ? 100 : 92 }))]));
  discovery.contacts.push(...[...posters.values()].flat());
  const result: OutreachResult = { contactsDiscovered: discovery.contacts.length, prepared: 0, completed: 0, duplicates: 0, sourcesAttempted: discovery.sourcesAttempted, sourcesWorking: discovery.sourcesWorking, errors: discovery.errors };
  const saveContact = db.prepare(`INSERT INTO contacts(company,name,title,linkedin_url,relevance_score) VALUES (@company,@name,@title,@profileUrl,@relevanceScore)
    ON CONFLICT(linkedin_url) DO UPDATE SET company=excluded.company,name=excluded.name,title=excluded.title,relevance_score=excluded.relevance_score`);
  for (const job of jobs) {
    // At most max_contacts_per_company people per company in total, however many jobs it has and however many runs have drafted for it.
    const places = Math.min(config.outreach.max_contacts_per_job, config.outreach.max_contacts_per_company - companyOutreachCount(db, job.company));
    if (places <= 0) continue;
    const candidates = rankContacts(discovery.contacts.filter((contact) => companyKey(contact.company) === companyKey(job.company))).filter((contact) => contact.relevanceScore >= 65 && !hasOutreach(db, contact.profileUrl));
    const mine = (posters.get(job.id) ?? []).filter((contact) => !hasOutreach(db, contact.profileUrl)).slice(0, places);
    for (const contact of [...mine, ...pickBalanced(candidates.filter((contact) => !mine.some((poster) => poster.profileUrl === contact.profileUrl)), places - mine.length)]) {
      try {
        db.transaction(() => {
          saveContact.run(contact);
          const draft = draftOutreach(db, config, job.id, contact, job, 'CONNECTION');
          if (!draft) { result.duplicates++; return; }
          const inserted = db.prepare(`INSERT OR IGNORE INTO outreach(job_id,contact_id,type,message,character_count,status,run_id,created_at)
            VALUES (?,(SELECT id FROM contacts WHERE linkedin_url=?),?,?,?,'OUTREACH_READY',?,?)`).run(job.id, contact.profileUrl, draft.type, draft.message, draft.characterCount, runId, new Date().toISOString());
          result.prepared += inserted.changes;
        })();
      } catch (error) { result.errors.push(`Job ${job.id}: ${error instanceof Error ? error.message : String(error)}`); }
    }
  }
  await logEvent(root, { runId, stage: 'outreach', action: 'prepared', result });
  return result;
}
