import { execFileSync } from 'node:child_process';
import { readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';

export type AuditFinding = { file: string; kind: string };

// Things that must never be in a public repository, whoever they belong to.
const SECRET_PATTERNS: Array<[string, RegExp]> = [
  ['GitHub token', /\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b/],
  ['private key', /-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----/],
  ['Google OAuth token', /"refresh_token"\s*:\s*"[^"]{20,}"|\bya29\.[A-Za-z0-9_-]{30,}/],
  ['API key', /\b(?:sk-[A-Za-z0-9]{32,}|AIza[0-9A-Za-z_-]{35}|AKIA[0-9A-Z]{16})\b/],
  ['LinkedIn session cookie', /\bli_at=[A-Za-z0-9_-]{40,}/]
];
// vault/private.enc is the encrypted private vault: the publish script leaves it out, so it is not a finding here.
const NEVER_PUBLISH = [/(^|\/)(credentials|token|drive_token)\.json$/, /(^|\/)config\.yaml$/, /(^|\/)profile\.yaml$/, /^config\/(candidate|contacts)\.json$/, /\.(?:db|db-wal|db-shm|b64)$/, /^browser-profile\//, /^data\/(?!\.gitkeep)/, /^resumes\/(?!\.gitkeep)/];

/** The identifying values in the candidate profile (never printed, only searched for). */
export function privateValues(candidate: Record<string, unknown>): string[] {
  const out: string[] = [];
  for (const key of ['email', 'phone', 'full_name', 'first_name', 'last_name', 'linkedin', 'portfolio', 'current_company', 'previous_companies']) {
    const value = candidate[key];
    for (const item of Array.isArray(value) ? value : [value]) if (typeof item === 'string' && item.trim().length >= 5) out.push(item.trim());
  }
  const phone = String(candidate.phone ?? '').replace(/\D/g, '');
  if (phone.length >= 8) out.push(phone.slice(-10));
  const name = String(candidate.full_name ?? '').trim().split(/\s+/);
  if (name.length > 1) out.push(name.slice(0, 2).join(' '), name.slice(0, 2).join('_'));
  return [...new Set(out.map((value) => value.toLowerCase()))];
}

/** Scans the files that would be published (tracked or untracked-but-not-ignored) for the candidate's own details and for credentials. */
export async function auditForPublication(root: string): Promise<{ checked: number; findings: AuditFinding[] }> {
  let candidate: Record<string, unknown> = {};
  try { candidate = JSON.parse(await readFile(join(root, 'config', 'candidate.json'), 'utf8')) as Record<string, unknown>; } catch { /* the example profile has nothing private */ }
  // The example profile (used by CI) holds placeholders, not a person: only the credential scan applies then.
  const values = /@example\.(?:com|org|net)$/i.test(String(candidate.email ?? '')) ? [] : privateValues(candidate);
  const files = execFileSync('git', ['ls-files', '-co', '--exclude-standard'], { cwd: root, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 }).split('\n').filter(Boolean);
  const findings: AuditFinding[] = [];
  let checked = 0;
  for (const file of files) {
    if (file === 'vault/private.enc') continue;
    if (NEVER_PUBLISH.some((pattern) => pattern.test(file))) { findings.push({ file, kind: 'a private file that must not be published' }); continue; }
    if (/\.(?:png|jpe?g|gif|pdf|xlsx|docx|zip|enc|ico)$/i.test(file) || file === 'package-lock.json') continue;
    const info = await stat(join(root, file)).catch(() => undefined);
    if (!info || !info.isFile() || info.size > 5 * 1024 * 1024) continue;
    const text = await readFile(join(root, file), 'utf8').catch(() => '');
    checked++;
    const lower = text.toLowerCase();
    if (values.some((value) => lower.includes(value))) findings.push({ file, kind: 'contains a detail from your candidate profile (name, phone, email, links or employers)' });
    for (const [kind, pattern] of SECRET_PATTERNS) if (pattern.test(text)) findings.push({ file, kind });
  }
  return { checked, findings };
}
