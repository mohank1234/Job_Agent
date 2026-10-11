import { createCipheriv, createDecipheriv, randomBytes, scryptSync } from 'node:crypto';
import { gunzipSync, gzipSync } from 'node:zlib';
import { mkdir, readFile, readdir, stat, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';

const MAGIC = Buffer.from('JLV1');
export type VaultEntry = { path: string; data: Buffer };

function derive(passphrase: string, salt: Buffer): Buffer { return scryptSync(passphrase, salt, 32, { N: 16384, r: 8, p: 1 }); }

/** Encrypts files into one authenticated blob. Safe to commit: without the passphrase it is opaque and tamper-evident. */
export function seal(entries: VaultEntry[], passphrase: string): Buffer {
  if (!passphrase) throw new Error('A vault passphrase is required (VAULT_PASSPHRASE)');
  const salt = randomBytes(16); const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', derive(passphrase, salt), iv);
  const payload = gzipSync(Buffer.from(JSON.stringify(entries.map((entry) => ({ p: entry.path.split(sep).join('/'), d: entry.data.toString('base64') })))));
  const body = Buffer.concat([cipher.update(payload), cipher.final()]);
  return Buffer.concat([MAGIC, salt, iv, cipher.getAuthTag(), body]);
}

export function open(blob: Buffer, passphrase: string): VaultEntry[] {
  if (blob.length < 48 || !blob.subarray(0, 4).equals(MAGIC)) throw new Error('Not a job-link vault file');
  const decipher = createDecipheriv('aes-256-gcm', derive(passphrase, blob.subarray(4, 20)), blob.subarray(20, 32));
  decipher.setAuthTag(blob.subarray(32, 48));
  let payload: Buffer;
  try { payload = Buffer.concat([decipher.update(blob.subarray(48)), decipher.final()]); }
  catch { throw new Error('Vault could not be decrypted: wrong passphrase or corrupted file'); }
  return (JSON.parse(gunzipSync(payload).toString('utf8')) as Array<{ p: string; d: string }>).map((item) => ({ path: item.p, data: Buffer.from(item.d, 'base64') }));
}

async function walk(root: string, target: string, out: VaultEntry[]): Promise<void> {
  const absolute = resolve(root, target);
  if (absolute !== resolve(root) && !absolute.startsWith(resolve(root) + sep)) throw new Error(`Refusing to read outside the project: ${target}`);
  const info = await stat(absolute);
  if (info.isDirectory()) { for (const name of (await readdir(absolute)).sort()) if (name !== '.gitkeep') await walk(root, join(target, name), out); return; }
  out.push({ path: relative(root, absolute).split(sep).join('/'), data: await readFile(absolute) });
}
export async function collect(root: string, targets: string[]): Promise<VaultEntry[]> { const out: VaultEntry[] = []; for (const target of targets) await walk(root, target, out); return out; }

/** Writes entries under root, refusing any path that would land outside it. */
export async function extract(root: string, entries: VaultEntry[]): Promise<string[]> {
  const written: string[] = [];
  for (const entry of entries) {
    if (isAbsolute(entry.path)) throw new Error(`Vault entry has an absolute path: ${entry.path}`);
    const destination = resolve(root, entry.path);
    if (!destination.startsWith(resolve(root) + sep)) throw new Error(`Vault entry escapes the project folder: ${entry.path}`);
    await mkdir(dirname(destination), { recursive: true }); await writeFile(destination, entry.data); written.push(entry.path);
  }
  return written;
}

export function vaultPassphrase(): string {
  const value = process.env.VAULT_PASSPHRASE?.trim();
  if (!value) throw new Error('VAULT_PASSPHRASE is not set. It is kept in the environment (a GitHub secret in CI, a user environment variable locally), never in the project folder.');
  return value;
}
