import type Database from 'better-sqlite3';
import type { CandidateAnswer } from './core/types.js';
import { canonicalFact, normalizeQuestion } from './ats/answers.js';

export function rememberVerifiedAnswer(db: Database.Database, question: string, answer: string): void {
  const fact = canonicalFact(question);
  if (!fact || !answer.trim()) return;
  db.prepare('INSERT INTO answer_memory(fact,question,answer,verified_at) VALUES(?,?,?,?) ON CONFLICT(fact) DO UPDATE SET question=excluded.question,answer=excluded.answer,verified_at=excluded.verified_at').run(fact,question,answer,new Date().toISOString());
}
export function loadAnswerMemory(db: Database.Database, answers: Record<string, CandidateAnswer>): number {
  // Existing ANSWERED records were explicitly provided through the answer command; import once per fact.
  for (const old of db.prepare("SELECT question,answer,answered_at FROM questions WHERE status='ANSWERED' AND answer IS NOT NULL AND category='MISSING' ORDER BY answered_at DESC,id DESC").all() as Array<{question:string;answer:string;answered_at:string}>) {
    const fact=canonicalFact(old.question);
    if(fact)db.prepare('INSERT OR IGNORE INTO answer_memory(fact,question,answer,verified_at) VALUES(?,?,?,?)').run(fact,old.question,old.answer,old.answered_at??new Date().toISOString());
  }
  const rows = db.prepare('SELECT fact,question,answer FROM answer_memory').all() as Array<{fact:string;question:string;answer:string}>;
  for (const row of rows) {
    const answer: CandidateAnswer = {value:row.answer,confidence:'configured'};
    for(const key of Object.keys(answers)) if(canonicalFact(key)===row.fact) answers[key]=answer;
    answers[`fact:${row.fact}`] = answer;
    answers[normalizeQuestion(row.question)] = answer;
    if (row.fact.startsWith('skill:')) answers[`skill years: ${row.fact.slice(6)}`] = answer;
    else if (!row.fact.includes(':')) answers[normalizeQuestion(row.fact)] = answer;
  }
  return rows.length;
}
