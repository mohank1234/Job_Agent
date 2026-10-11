import Database from 'better-sqlite3';
import { mkdir, readFile, readdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { PDFParse } from 'pdf-parse';
import { z } from 'zod';
import { extractSkills } from './fit.js';

export type ResumeRecord = { id?: number; filename: string; category: 'GENERAL_QA' | 'SENIOR_QA' | 'MAIN'; keywords: string[]; selectionReason?: string; contentHash?: string; extractionError?: string };
const resumeSettingsSchema = z.object({ overrides: z.array(z.object({ filename: z.string().min(1), category: z.enum(['GENERAL_QA', 'SENIOR_QA', 'MAIN']), keywords: z.array(z.string()).default([]) }).strict()).default([]) }).strict();

export async function scanResumes(root: string, db: Database.Database): Promise<ResumeRecord[]> {
  await mkdir(join(root, 'resumes'), { recursive: true });
  const raw = await readFile(join(root, 'config', 'resumes.json'), 'utf8').catch((error: NodeJS.ErrnoException) => { if (error.code === 'ENOENT') return '{"overrides":[]}'; throw error; });
  const settings = resumeSettingsSchema.parse(JSON.parse(raw));
  const files = (await readdir(join(root, 'resumes'))).filter((file) => file.toLowerCase().endsWith('.pdf')).sort();
  const records: ResumeRecord[] = [];
  const insert = db.prepare(`INSERT INTO resumes (filename, category, keywords, updated_at, content_hash, file_size, available, extracted_keywords, extraction_error)
    VALUES (@filename, @category, @keywords, @updatedAt, @contentHash, @fileSize, 1, @extractedKeywords, @extractionError)
    ON CONFLICT(filename) DO UPDATE SET category=excluded.category, keywords=excluded.keywords, updated_at=excluded.updated_at,
    content_hash=excluded.content_hash, file_size=excluded.file_size, available=1, extracted_keywords=excluded.extracted_keywords, extraction_error=excluded.extraction_error`);
  // Parse before changing availability, preserving the previous registry if a scan is interrupted.
  const pending: Array<{ record: ResumeRecord; fileSize: number; extractedKeywords: string[] }> = [];
  for (const filename of files) {
    const data = await readFile(join(root, 'resumes', filename));
    const contentHash = createHash('sha256').update(data).digest('hex');
    let text = ''; let extractionError: string | undefined; let extractedKeywords: string[] = [];
    // Re-extract on each scan so improved classifiers also apply to unchanged files.
    {
      let parser: PDFParse | undefined;
      try { parser = new PDFParse({ data: new Uint8Array(data) }); text = (await parser.getText()).text; extractedKeywords = extractSkills(text); if (!text.trim()) extractionError = 'PDF has no extractable text; content matching unavailable.'; }
      catch (error) { extractionError = `PDF extraction failed: ${error instanceof Error ? error.message : String(error)}`; }
      finally { await parser?.destroy(); }
    }
    const override = settings.overrides.find((item) => item.filename === filename);
    const category = override?.category ?? classify(filename);
    const record: ResumeRecord = { filename, category, keywords: [...new Set([...extractedKeywords, ...(override?.keywords ?? [])])], contentHash, extractionError };
    pending.push({ record, fileSize: data.length, extractedKeywords });
  }
  db.transaction(() => {
    db.prepare('UPDATE resumes SET available = 0').run();
    for (const { record, fileSize, extractedKeywords } of pending) {
      insert.run({ ...record, keywords: JSON.stringify(record.keywords), updatedAt: new Date().toISOString(), fileSize, extractedKeywords: JSON.stringify(extractedKeywords), extractionError: record.extractionError ?? null });
      record.id = (db.prepare('SELECT id FROM resumes WHERE filename = ?').get(record.filename) as { id: number }).id;
      records.push(record);
    }
  })();
  return records;
}

export function selectResume(db: Database.Database, title: string, description: string): ResumeRecord | undefined {
  const desired = extractSkills(`${title} ${description}`);
  const aiRole = /\b(?:llm|rag|genai|ai agent|promptfoo|generative ai|ai testing)\b/i.test(`${title} ${description}`);
  const seniorRole = /\b(?:sr\.?|senior|lead|staff|principal)\b/i.test(title);
  const candidates = db.prepare('SELECT id, filename, category, keywords, content_hash, extraction_error FROM resumes WHERE available = 1 ORDER BY filename').all() as Array<{ id: number; filename: string; category: ResumeRecord['category']; keywords: string; content_hash: string; extraction_error: string | null }>;
  const ranked = candidates.map((row) => {
    const keywords = JSON.parse(row.keywords) as string[];
    const overlap = desired.filter((skill) => keywords.includes(skill));
    const aiOverlap = overlap.filter((skill) => /LLM|RAG|GenAI|AI agent|Prompt/i.test(skill));
    let rank = overlap.length * 4 + (aiRole ? aiOverlap.length * 6 : 0);
    if (seniorRole && row.category === 'SENIOR_QA') rank += 3;
    if (aiRole && row.category === 'MAIN') rank += 2;
    if (!seniorRole && !aiRole && row.category === 'GENERAL_QA') rank += 2;
    if (row.extraction_error) rank -= 20;
    const record: ResumeRecord = { id: row.id, filename: row.filename, category: row.category, keywords, contentHash: row.content_hash, extractionError: row.extraction_error ?? undefined,
      selectionReason: `JD content overlap: ${overlap.join(', ') || 'none'}. ${aiRole ? 'AI testing emphasis. ' : ''}${seniorRole ? 'Senior title considered. ' : ''}Category: ${row.category}.${row.extraction_error ? ` ${row.extraction_error}` : ''}` };
    return { rank, record };
  }).sort((a, b) => b.rank - a.rank || a.record.filename.localeCompare(b.record.filename));
  return ranked[0]?.record;
}

function classify(filename: string): ResumeRecord['category'] {
  if (/(?:^|[ _.-])(?:sr|senior)(?:[ _.-]|$)/i.test(filename)) return 'SENIOR_QA';
  if (/(?:^|[ _.-])main(?:[ _.-]|$)/i.test(filename)) return 'MAIN';
  return 'GENERAL_QA';
}
