import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ExcelJS from 'exceljs';
import { loadConfig } from '../src/config.js';
import { openDatabase } from '../src/database.js';
import { collectDashboard, renderDashboard } from '../src/dashboard.js';
import { jobOutreachComplete, writeOutreachWorkbook } from '../src/excel-export.js';

const config = await loadConfig(process.cwd());

test('a job counts as complete only when every person written to is finished (sent, pending, connected or declined)', () => {
  assert.equal(jobOutreachComplete([]), false);
  assert.equal(jobOutreachComplete(['SENT', 'PENDING', 'ALREADY_CONNECTED', 'DECLINED']), true);
  assert.equal(jobOutreachComplete(['SENT', 'OUTREACH_READY']), false);
  assert.equal(jobOutreachComplete(['SENT', 'FAILED']), false);
});

test('the workbook lists finished jobs separately from jobs still waiting, and the dashboard renders both', async () => {
  const root = await mkdtemp(join(tmpdir(), 'job-link-excel-'));
  const db = openDatabase(root);
  try {
    const now = new Date().toISOString();
    const job = (title: string, location: string): number => Number(db.prepare("INSERT INTO jobs(company,title,location,job_url,source,detected_at,description,fit_score,status,evidence_mode,posted_at,updated_at) VALUES('Acme',?,?,?,'linkedin',?,'QA',80,'REVIEW','live',?,?)").run(title, location, `https://example.com/${title}`, now, now, now).lastInsertRowid);
    const person = (name: string): number => Number(db.prepare("INSERT INTO contacts(company,name,title,linkedin_url,relevance_score) VALUES('Acme',?,'Recruiter',?,90)").run(name, `https://linkedin.com/in/${name}`).lastInsertRowid);
    const note = (jobId: number, contactId: number, status: string): void => { db.prepare("INSERT INTO outreach(job_id,contact_id,type,message,character_count,status,created_at) VALUES(?,?,'CONNECTION','hello',5,?,?)").run(jobId, contactId, status, now); };
    const done = job('Done', 'Hyderabad'); const waiting = job('Waiting', 'Pune');
    note(done, person('a'), 'SENT'); note(waiting, person('b'), 'SENT'); note(waiting, person('c'), 'OUTREACH_READY');
    const result = await writeOutreachWorkbook(root, db, config.search.location_priority);
    assert.deepEqual([result.completed, result.inProgress, result.people], [1, 1, 3]);
    const book = new ExcelJS.Workbook(); await book.xlsx.readFile(result.file);
    assert.deepEqual(book.worksheets.map((sheet) => sheet.name), ['Completed jobs', 'Outreach in progress', 'People contacted']);
    assert.equal(book.getWorksheet('Completed jobs')!.getRow(2).getCell(3).value, 'Done');
    assert.equal(book.getWorksheet('Completed jobs')!.getRow(2).getCell(5).value, 'Hyderabad');
    assert.equal(book.getWorksheet('Outreach in progress')!.getRow(2).getCell(3).value, 'Waiting');
    const html = renderDashboard(collectDashboard(db, config, {}, Date.now(), false));
    assert.match(html, /Job Hub/); assert.match(html, /Open jobs by age and location/); assert.match(html, /0-1h/);
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});
