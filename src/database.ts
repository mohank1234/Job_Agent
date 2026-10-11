import Database from 'better-sqlite3';
import { mkdirSync } from 'node:fs';

export function openDatabase(root: string): Database.Database {
  mkdirSync(`${root}/data`, { recursive: true });
  const db = new Database(`${root}/data/job-link.db`);
  db.pragma('journal_mode = WAL');
  db.pragma('foreign_keys = ON');
  db.pragma('busy_timeout = 5000');
  db.exec(`
    CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL, location TEXT, work_mode TEXT, job_url TEXT NOT NULL UNIQUE, source TEXT NOT NULL, posted_at TEXT, detected_at TEXT NOT NULL, description TEXT NOT NULL, salary TEXT, experience_requirement TEXT, required_skills TEXT NOT NULL DEFAULT '[]', preferred_skills TEXT NOT NULL DEFAULT '[]', application_url TEXT, fit_score INTEGER, fit_reason TEXT, status TEXT NOT NULL DEFAULT 'NEW', UNIQUE(company, title, job_url));
    CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id), application_url TEXT, resume TEXT, started_at TEXT, submitted_at TEXT, status TEXT NOT NULL, confirmation TEXT, screenshot TEXT);
    CREATE TABLE IF NOT EXISTS resumes (id INTEGER PRIMARY KEY, filename TEXT NOT NULL UNIQUE, category TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY, company TEXT NOT NULL, name TEXT NOT NULL, title TEXT NOT NULL, linkedin_url TEXT NOT NULL UNIQUE, relevance_score INTEGER NOT NULL, connection_status TEXT NOT NULL DEFAULT 'UNKNOWN');
    CREATE TABLE IF NOT EXISTS outreach (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), contact_id INTEGER NOT NULL REFERENCES contacts(id), type TEXT NOT NULL, message TEXT NOT NULL, character_count INTEGER NOT NULL, sent_at TEXT, status TEXT NOT NULL, UNIQUE(job_id, contact_id, type));
    CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), question TEXT NOT NULL, answer TEXT, requires_user_input INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN');
    CREATE TABLE IF NOT EXISTS answer_memory (fact TEXT PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL, verified_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS errors (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL, job_id INTEGER, error TEXT NOT NULL, screenshot TEXT, timestamp TEXT NOT NULL);
  `);
  // Additive migrations preserve the original database, application evidence and IDs.
  const additions: Record<string, Record<string, string>> = {
    runs: { metrics_json: "TEXT NOT NULL DEFAULT '{}'", mode: "TEXT NOT NULL DEFAULT 'legacy'" },
    jobs: { canonical_url: 'TEXT', external_id: 'TEXT', ats_job_id: 'TEXT', identity_key: 'TEXT', metadata: "TEXT NOT NULL DEFAULT '{}'", evidence_mode: "TEXT NOT NULL DEFAULT 'live'", filter_reason: 'TEXT', fit_details: 'TEXT', updated_at: 'TEXT' },
    applications: { resume_id: 'INTEGER REFERENCES resumes(id)', resume_reason: 'TEXT', ats: 'TEXT', attempt_count: 'INTEGER NOT NULL DEFAULT 0', last_stage: 'TEXT', last_error: 'TEXT', updated_at: 'TEXT', run_id: 'TEXT', final_url: 'TEXT', evidence: "TEXT NOT NULL DEFAULT '{}'", retryable: 'INTEGER NOT NULL DEFAULT 0', next_retry_at: 'TEXT' },
    resumes: { content_hash: 'TEXT', file_size: 'INTEGER', available: 'INTEGER NOT NULL DEFAULT 1', extracted_keywords: "TEXT NOT NULL DEFAULT '[]'", extraction_error: 'TEXT' },
    questions: { field_key: 'TEXT', category: 'TEXT', page_url: 'TEXT', created_at: 'TEXT', answered_at: 'TEXT' },
    outreach: { run_id: 'TEXT', created_at: 'TEXT', screenshot: 'TEXT', prepared_at: 'TEXT', connection_state: 'TEXT' }
  };
  db.transaction(() => {
    for (const [table, columns] of Object.entries(additions)) {
      const existing = new Set((db.pragma(`table_info(${table})`) as Array<{name: string}>).map(row => row.name));
      for (const [name, definition] of Object.entries(columns)) if (!existing.has(name)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${name} ${definition}`);
    }
    db.exec(`
      CREATE TABLE IF NOT EXISTS job_identities (identity TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id));
      CREATE INDEX IF NOT EXISTS jobs_identity_idx ON jobs(identity_key);
      CREATE INDEX IF NOT EXISTS jobs_queue_idx ON jobs(status, posted_at, fit_score);
      CREATE TABLE IF NOT EXISTS run_sources (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), name TEXT NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, discovered INTEGER NOT NULL DEFAULT 0, error TEXT, url TEXT, screenshot TEXT, truncated INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS run_jobs (run_id TEXT NOT NULL REFERENCES runs(id), job_id INTEGER NOT NULL REFERENCES jobs(id), disposition TEXT, age_bucket TEXT, score INTEGER, queued INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0, application_opened INTEGER NOT NULL DEFAULT 0, forms_inspected INTEGER NOT NULL DEFAULT 0, fields_filled INTEGER NOT NULL DEFAULT 0, resume_uploaded INTEGER NOT NULL DEFAULT 0, application_status TEXT, reason TEXT, PRIMARY KEY(run_id, job_id));
      CREATE TABLE IF NOT EXISTS submission_ledger (job_id INTEGER PRIMARY KEY REFERENCES jobs(id), run_id TEXT NOT NULL, started_at TEXT NOT NULL, confirmed_at TEXT, confirmation TEXT);
      CREATE TABLE IF NOT EXISTS application_events (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), run_id TEXT, from_status TEXT, to_status TEXT NOT NULL, stage TEXT NOT NULL, reason TEXT, timestamp TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS linkedin_actions (id INTEGER PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS linkedin_actions_day_idx ON linkedin_actions(day);
      CREATE TABLE IF NOT EXISTS linkedin_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      PRAGMA user_version = 2;
    `);
    // Historical fixture records must never enter the live application queue.
    db.prepare("UPDATE jobs SET evidence_mode='fixture' WHERE source LIKE 'controlled%' OR job_url LIKE 'https://example.com/%'").run();
  })();
  return db;
}
