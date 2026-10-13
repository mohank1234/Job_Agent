import type Database from 'better-sqlite3';
import type { CandidateAnswer } from './core/types.js';
import { canonicalFact, normalizeQuestion } from './ats/answers.js';

export function rememberVerifiedAnswer(db: Database.Database, question: string, answer: string): void {
  const fact = canonicalFact(question);
  if (!fact || !answer.trim()) return;
  db.prepare('INSERT INTO answer_memory(fact,question,answer,verified_at) VALUES(?,?,?,?) ON CONFLICT(fact) DO UPDATE SET question=excluded.question,answer=excluded.answer,verified_at=excluded.verified_at').run(fact,question,answer,new Date().toISOString());
}
/** Questions whose answer depends on the employer, the country or a legal statement: asked every time, never reused automatically. */
export function isSensitiveQuestion(question: string): boolean {
  return /\b(?:authori[sz]ed|authori[sz]ation|visa|sponsor\w*|citizen\w*|agree\w*|consent\w*|certify|acknowledg\w*|willing\w*|relocat\w*|shift|disabilit\w*|gender|race|ethnic\w*|veteran|religio\w*|marital|pregnan\w*|criminal|convict\w*|background check|drug|terms|privacy|signature)\b/i.test(question);
}
/** Turns what the person typed into what a form expects: y/n become Yes/No for yes-no questions. */
export function normalizeTypedAnswer(question: string, typed: string): string {
  const text = typed.trim();
  const yesNo = /^(?:are|do|does|did|have|has|will|would|can|could|is|was)\b/i.test(question.trim()) || /\b(?:yes\/no|yes or no)\b/i.test(question);
  if (yesNo && /^y(?:es)?$/i.test(text)) return 'Yes';
  if (yesNo && /^n(?:o)?$/i.test(text)) return 'No';
  return text;
}
/** Saves an answer for next time: plain facts under their name (notice period, years with a skill, CTC ...), every other question under its own wording. Sensitive questions are never reused. */
export function rememberAnswer(db: Database.Database, question: string, answer: string): 'fact' | 'question' | 'not-reused' {
  if (!answer.trim()) return 'not-reused';
  if (canonicalFact(question)) { rememberVerifiedAnswer(db, question, answer); return 'fact'; }
  if (isSensitiveQuestion(question)) return 'not-reused';
  const key = `q:${normalizeQuestion(question)}`;
  db.prepare('INSERT INTO answer_memory(fact,question,answer,verified_at) VALUES(?,?,?,?) ON CONFLICT(fact) DO UPDATE SET question=excluded.question,answer=excluded.answer,verified_at=excluded.verified_at').run(key, question, answer, new Date().toISOString());
  return 'question';
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
