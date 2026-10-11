import { execFileSync } from 'node:child_process';
import { copyFile, mkdir, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import Database from 'better-sqlite3';
import { collect, extract, open, seal, type VaultEntry } from './vault.js';

const STATE_FILE = 'state.enc';
const STATE_BRANCH = 'state';
/** Where the encrypted state lives: `origin` normally, or the git remote named by JOB_LINK_DATA_REMOTE (a separate private repository, so a public code repository never holds any state). */
const remote = (): string => process.env.JOB_LINK_DATA_REMOTE || 'origin';
const tracked = ['config/contacts.json'];

/** A consistent single-file snapshot of the live database plus the contacts list, encrypted. */
export async function packState(root: string, passphrase: string): Promise<Buffer> {
  await mkdir(join(root, 'data'), { recursive: true });
  const snapshot = join(root, 'data', 'state-snapshot.db');
  await rm(snapshot, { force: true });
  const db = new Database(join(root, 'data', 'job-link.db'), { readonly: true, fileMustExist: true });
  try { await db.backup(snapshot); } finally { db.close(); }
  try {
    const entries: VaultEntry[] = [{ path: 'data/job-link.db', data: await readFile(snapshot) }];
    for (const extra of tracked) { try { entries.push(...await collect(root, [extra])); } catch { /* optional file */ } }
    return seal(entries, passphrase);
  } finally { await rm(snapshot, { force: true }); }
}

export async function unpackState(root: string, blob: Buffer, passphrase: string, force: boolean): Promise<string[]> {
  const entries = open(blob, passphrase);
  const local = join(root, 'data', 'job-link.db');
  const exists = await stat(local).then(() => true, () => false);
  if (exists && !force) throw new Error('A local database already exists; re-run with --force to replace it (a backup copy is kept).');
  if (exists) {
    await copyFile(local, join(root, 'data', `job-link.before-state-pull.${Date.now()}.db`));
    // A scheduled pull runs daily; keep only the three most recent safety copies.
    const backups = (await readdir(join(root, 'data'))).filter((name) => /^job-link\.before-state-pull\.\d+\.db$/.test(name)).sort();
    for (const stale of backups.slice(0, -3)) await rm(join(root, 'data', stale), { force: true });
  }
  // Stale write-ahead files from the previous database must not be replayed onto the restored one.
  for (const suffix of ['-wal', '-shm']) await rm(`${local}${suffix}`, { force: true });
  return extract(root, entries);
}

const git = (root: string, args: string[], input?: Buffer): Buffer => execFileSync('git', args, { cwd: root, input, stdio: ['pipe', 'pipe', 'pipe'], maxBuffer: 512 * 1024 * 1024 });
const baseFile = (root: string): string => join(root, 'data', 'state-base.txt');

/** Latest encrypted state from the `state` branch of origin, or undefined when none has been published yet.
 * The commit that was read is remembered so a later push can refuse to overwrite anything newer. */
export async function fetchRemoteState(root: string): Promise<Buffer | undefined> {
  await mkdir(join(root, 'data'), { recursive: true });
  let blob: Buffer | undefined; let commit = '';
  try {
    git(root, ['fetch', '--depth=1', remote(), STATE_BRANCH]);
    commit = git(root, ['rev-parse', 'FETCH_HEAD']).toString().trim();
    blob = git(root, ['show', `FETCH_HEAD:${STATE_FILE}`]);
  } catch { blob = undefined; commit = ''; }
  await writeFile(baseFile(root), commit);
  return blob;
}

/** Publishes the blob as a single history-free commit on `state`, without touching the working tree or the current branch. */
export async function pushRemoteState(root: string, blob: Buffer): Promise<string> {
  const object = git(root, ['hash-object', '-w', '--stdin'], blob).toString().trim();
  const tree = git(root, ['mktree'], Buffer.from(`100644 blob ${object}\t${STATE_FILE}\n`)).toString().trim();
  const commit = git(root, ['-c', 'user.name=job-link', '-c', 'user.email=job-link@users.noreply.github.com', 'commit-tree', tree, '-m', `state ${new Date().toISOString()}`]).toString().trim();
  // The commit this database was restored from (empty = nothing existed then). Anything newer on the remote is someone else's work.
  const expected = (await readFile(baseFile(root), 'utf8').catch(() => '')).trim();
  try { git(root, ['push', `--force-with-lease=refs/heads/${STATE_BRANCH}:${expected}`, remote(), `${commit}:refs/heads/${STATE_BRANCH}`]); }
  catch (error) { throw new Error(`Remote state changed since it was last pulled, so it was not overwritten. Run state-pull --force first (your local database is backed up). ${(error as { stderr?: Buffer }).stderr?.toString().trim() ?? ''}`); }
  await writeFile(baseFile(root), commit);
  return commit;
}
