import ExcelJS from 'exceljs';
import type Database from 'better-sqlite3';
import { mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { audienceOf } from './outreach.js';
import { ageBatch, locationTier, locationTierLabel } from './priority.js';

type OutreachRow = { outreach_id: number; job_id: number; company: string; title: string; location: string | null; work_mode: string | null; job_url: string; posted_at: string | null; fit_score: number | null; job_status: string;
  contact_name: string; contact_title: string; profile_url: string; status: string; sent_at: string | null; character_count: number; message: string };

// An invitation that was sent, is already pending, or reaches someone already connected is finished; so is a person the user said "never" for.
const FINISHED = new Set(['SENT', 'PENDING', 'ALREADY_CONNECTED', 'DECLINED']);
export const jobOutreachComplete = (statuses: string[]): boolean => statuses.length > 0 && statuses.every((status) => FINISHED.has(status));

/** Writes reports/job-outreach.xlsx: one row per job whose outreach is finished ("Completed jobs"), the jobs still waiting on a send, and every person written to. */
export async function writeOutreachWorkbook(root: string, db: Database.Database, priority: string[], file = join(root, 'reports', 'job-outreach.xlsx')): Promise<{ file: string; completed: number; inProgress: number; people: number }> {
  const rows = db.prepare(`SELECT o.id outreach_id, j.id job_id, j.company, j.title, j.location, j.work_mode, j.job_url, j.posted_at, j.fit_score, j.status job_status,
      c.name contact_name, c.title contact_title, c.linkedin_url profile_url, o.status, o.sent_at, o.character_count, o.message
    FROM outreach o JOIN jobs j ON j.id = o.job_id JOIN contacts c ON c.id = o.contact_id WHERE j.evidence_mode = 'live' ORDER BY j.id, o.id`).all() as OutreachRow[];
  const byJob = new Map<number, OutreachRow[]>();
  for (const row of rows) byJob.set(row.job_id, [...(byJob.get(row.job_id) ?? []), row]);

  const book = new ExcelJS.Workbook(); book.creator = 'Job Link';
  const header = (sheet: ExcelJS.Worksheet, columns: Array<[string, number]>): void => {
    sheet.columns = columns.map(([name, width]) => ({ header: name, key: name, width }));
    sheet.getRow(1).font = { bold: true }; sheet.views = [{ state: 'frozen', ySplit: 1 }];
  };
  const jobSheet = (name: string, jobs: OutreachRow[][]): void => {
    const sheet = book.addWorksheet(name);
    header(sheet, [['Job ID', 8], ['Company', 24], ['Title', 38], ['Location', 24], ['Location priority', 16], ['Posted (UTC)', 20], ['Age batch', 14], ['Fit score', 9], ['People', 8], ['Sent', 7], ['Pending/connected', 17], ['Last outreach (UTC)', 20], ['Job link', 50]]);
    for (const people of jobs) {
      const job = people[0]; const tier = locationTier(job.location, job.work_mode, priority);
      const last = people.map((person) => person.sent_at).filter(Boolean).sort().at(-1) ?? '';
      sheet.addRow({ 'Job ID': job.job_id, Company: job.company, Title: job.title, Location: job.location ?? '', 'Location priority': locationTierLabel(tier, priority), 'Posted (UTC)': job.posted_at ?? '', 'Age batch': ageBatch(job.posted_at), 'Fit score': job.fit_score ?? '',
        People: people.length, Sent: people.filter((person) => person.status === 'SENT').length, 'Pending/connected': people.filter((person) => ['PENDING', 'ALREADY_CONNECTED'].includes(person.status)).length, 'Last outreach (UTC)': last, 'Job link': job.job_url });
    }
    sheet.autoFilter = { from: 'A1', to: 'M1' };
  };
  const complete = [...byJob.values()].filter((people) => jobOutreachComplete(people.map((person) => person.status)));
  const open = [...byJob.values()].filter((people) => !jobOutreachComplete(people.map((person) => person.status)));
  jobSheet('Completed jobs', complete); jobSheet('Outreach in progress', open);

  const people = book.addWorksheet('People contacted');
  header(people, [['Job ID', 8], ['Company', 24], ['Job title', 34], ['Person', 24], ['Their role', 36], ['Reads as', 10], ['Status', 18], ['Sent (UTC)', 20], ['Characters', 11], ['Profile', 46], ['Note', 80]]);
  for (const row of rows) people.addRow({ 'Job ID': row.job_id, Company: row.company, 'Job title': row.title, Person: row.contact_name, 'Their role': row.contact_title, 'Reads as': audienceOf(row.contact_title), Status: row.status, 'Sent (UTC)': row.sent_at ?? '', Characters: row.character_count, Profile: row.profile_url, Note: row.message });
  people.getColumn('Note').alignment = { wrapText: true, vertical: 'top' };

  await mkdir(dirname(file), { recursive: true });
  await book.xlsx.writeFile(file);
  return { file, completed: complete.length, inProgress: open.length, people: rows.length };
}
