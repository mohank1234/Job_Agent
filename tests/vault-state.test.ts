import assert from 'node:assert/strict';
import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { collect, extract, open, seal } from '../src/vault.js';
import { fetchRemoteState, packState, pushRemoteState, unpackState } from '../src/state-sync.js';
import { openDatabase } from '../src/database.js';
import { buildDigest } from '../src/digest.js';
import { loadConfig } from '../src/config.js';

const tmp = (name: string) => mkdtemp(join(tmpdir(), `job-link-${name}-`));

test('vault round-trips files, rejects a wrong passphrase, and detects tampering', async () => {
  const blob = seal([{ path: 'resumes/a.pdf', data: Buffer.from('%PDF secret résumé') }, { path: 'config/candidate.json', data: Buffer.from('{"full_name":"X"}') }], 'correct horse');
  assert.ok(!blob.includes(Buffer.from('secret')), 'plaintext must not appear in the blob');
  assert.deepEqual(open(blob, 'correct horse').map((entry) => entry.path), ['resumes/a.pdf', 'config/candidate.json']);
  assert.throws(() => open(blob, 'wrong'), /wrong passphrase or corrupted/);
  const tampered = Buffer.from(blob); tampered[tampered.length - 1] ^= 0xff;
  assert.throws(() => open(tampered, 'correct horse'), /wrong passphrase or corrupted/);
  assert.throws(() => open(Buffer.from('nonsense'), 'x'), /Not a job-link vault/);
  assert.throws(() => seal([], ''), /passphrase is required/);
});

test('vault extraction and collection never leave the project folder', async () => {
  const root = await tmp('vault-paths');
  try {
    await assert.rejects(extract(root, [{ path: '../escape.txt', data: Buffer.from('x') }]), /escapes the project folder/);
    await assert.rejects(extract(root, [{ path: join(tmpdir(), 'abs.txt'), data: Buffer.from('x') }]), /absolute path|escapes/);
    await assert.rejects(collect(root, ['../../etc']), /outside the project/);
    await mkdir(join(root, 'resumes')); await writeFile(join(root, 'resumes', 'r.pdf'), 'pdf'); await writeFile(join(root, 'resumes', '.gitkeep'), '');
    assert.deepEqual((await collect(root, ['resumes'])).map((entry) => entry.path), ['resumes/r.pdf']);
    assert.deepEqual(await extract(root, open(seal(await collect(root, ['resumes']), 'p'), 'p')), ['resumes/r.pdf']);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('state packs a consistent database snapshot and restores it, backing up any existing database', async () => {
  const source = await tmp('state-src'); const target = await tmp('state-dst');
  try {
    const db = openDatabase(source);
    db.prepare("INSERT INTO jobs(company,title,job_url,source,detected_at,description,status) VALUES('Acme','QA Engineer','https://example.test/1','live',?,'d','REVIEW')").run(new Date().toISOString());
    db.close();
    await mkdir(join(source, 'config')); await writeFile(join(source, 'config', 'contacts.json'), '{"contacts":[],"sources":[]}');
    const blob = await packState(source, 'pw');
    assert.deepEqual(await readdir(join(source, 'data')).then((names) => names.filter((name) => name.includes('snapshot'))), []);
    assert.ok((await unpackState(target, blob, 'pw', false)).includes('data/job-link.db'));
    const restored = openDatabase(target);
    assert.equal((restored.prepare('SELECT COUNT(*) n FROM jobs').get() as { n: number }).n, 1);
    restored.close();
    await assert.rejects(unpackState(target, blob, 'pw', false), /--force/);
    await unpackState(target, blob, 'pw', true);
    assert.equal((await readdir(join(target, 'data'))).filter((name) => name.startsWith('job-link.before-state-pull.')).length, 1);
  } finally { await rm(source, { recursive: true, force: true }); await rm(target, { recursive: true, force: true }); }
});

const git = (cwd: string, ...args: string[]) => execFileSync('git', args, { cwd, stdio: 'pipe' }).toString();
async function repoWithRemote(remote: string, name: string): Promise<string> {
  const dir = await tmp(name); git(dir, 'init', '-q'); git(dir, 'remote', 'add', 'origin', pathToFileURL(remote).href); return dir;
}

test('published state is history-free, round-trips through a real git remote, and a stale copy can never overwrite newer state', async () => {
  const remote = await tmp('remote'); git(remote, 'init', '-q', '--bare');
  const a = await repoWithRemote(remote, 'a'); const b = await repoWithRemote(remote, 'b');
  try {
    assert.equal(await fetchRemoteState(a), undefined);                       // nothing published yet
    const one = Buffer.from('state-one');
    await pushRemoteState(a, one);
    assert.deepEqual(await fetchRemoteState(b), one);                          // b restores exactly what a published
    await pushRemoteState(b, Buffer.from('state-two'));                        // b was up to date: allowed
    await assert.rejects(pushRemoteState(a, Buffer.from('stale-three')), /not overwritten/); // a is behind: refused
    assert.deepEqual(await fetchRemoteState(a), Buffer.from('state-two'));     // and nothing was lost
    await pushRemoteState(a, Buffer.from('state-three'));                      // after pulling, a may publish
    assert.equal(git(remote, 'rev-list', '--count', 'state').trim(), '1');     // one commit, no growing history
    assert.equal(git(remote, 'ls-tree', '--name-only', 'state').trim(), 'state.enc');
  } finally { for (const dir of [remote, a, b]) await rm(dir, { recursive: true, force: true }); }
});

test('restoring state keeps only the three newest safety copies of the local database', async () => {
  const source = await tmp('prune-src'); const target = await tmp('prune-dst');
  try {
    openDatabase(source).close();
    const blob = await packState(source, 'pw');
    await unpackState(target, blob, 'pw', false);
    for (let i = 0; i < 6; i++) { await new Promise((resolve) => setTimeout(resolve, 5)); await unpackState(target, blob, 'pw', true); }
    assert.equal((await readdir(join(target, 'data'))).filter((name) => name.startsWith('job-link.before-state-pull.')).length, 3);
  } finally { await rm(source, { recursive: true, force: true }); await rm(target, { recursive: true, force: true }); }
});

test('the digest is a persistent to-do list from the latest pipeline run: people-search runs are ignored and open items carry over until closed', async () => {
  const root = await tmp('digest'); const db = openDatabase(root);
  try {
    const config = await loadConfig(process.cwd());
    const base = Date.parse('2026-09-20T05:00:00Z'); const at = (minutes: number) => new Date(base + minutes * 60_000).toISOString();
    db.prepare("INSERT INTO runs(id,started_at,finished_at,status,mode,metrics_json) VALUES('run-1',?,?,'PARTIAL','live',?)").run(at(0), at(3), JSON.stringify({ discovered: 120, fresh8: 3, fresh24: 2, scored: 4, eligible: 2 }));
    db.prepare("INSERT INTO run_sources(run_id,name,type,status,discovered,error) VALUES('run-1','Some board','greenhouse','FAILED',0,'HTTP 404')").run();
    const job = (url: string, title: string, status: string) => Number(db.prepare("INSERT INTO jobs(company,title,job_url,source,detected_at,description,status,fit_score,evidence_mode) VALUES('Acme',?,?,'live',?,'d',?,88,'live')").run(title, url, at(0), status).lastInsertRowid);
    const app = (id: number, status: string, ats: string) => db.prepare("INSERT INTO applications(job_id,application_url,resume,status,ats) VALUES(?,?,'Main.pdf',?,?)").run(id, 'https://example.test/apply', status, ats);
    const question = (id: number, category: string) => db.prepare("INSERT INTO questions(job_id,question,requires_user_input,status,category,created_at) VALUES(?,'q',1,'OPEN',?,?)").run(id, category, at(0));
    const ready = job('https://example.test/ready', 'QA Engineer', 'APPLICATION_READY'); app(ready, 'READY_FOR_SUBMISSION', 'greenhouse');
    const easy = job('https://www.linkedin.com/jobs/view/9', 'SDET', 'NEEDS_USER_INPUT'); app(easy, 'NEEDS_USER_INPUT', 'linkedin'); question(easy, 'MANUAL_APPLY');
    const noForm = job('https://www.linkedin.com/jobs/view/10', 'QA Automation Engineer', 'NEEDS_USER_INPUT'); app(noForm, 'NEEDS_USER_INPUT', 'generic'); question(noForm, 'MANUAL_APPLY');
    const asks = job('https://example.test/asks', 'Test Lead', 'NEEDS_USER_INPUT'); app(asks, 'NEEDS_USER_INPUT', 'greenhouse'); question(asks, 'MISSING');
    const waiting = job('https://example.test/waiting', 'QA Analyst', 'REVIEW');
    db.prepare("INSERT INTO run_jobs(run_id,job_id,disposition,score) VALUES('run-1',?,'ELIGIBLE',88)").run(waiting);
    // A later people-search run has no jobs at all; it must not be mistaken for the run being summarised.
    db.prepare("INSERT INTO runs(id,started_at,status,mode,metrics_json) VALUES('people-1',?,'STARTED','people-search','{}')").run(at(10));

    const digest = buildDigest(db, config);
    assert.equal(digest.notable, true);
    assert.deepEqual([digest.counts.readyToSubmit, digest.counts.manualApply, digest.counts.needsInput, digest.counts.sourcesFailing], [1, 2, 1, 1]);
    assert.match(digest.markdown, /Run \`run-1\`/); assert.match(digest.markdown, /review \d+ --submit/); assert.match(digest.markdown, /Apply manually/); assert.match(digest.markdown, /Some board/); assert.match(digest.markdown, /not yet processed/);

    // The next day's discovery run finds nothing new; the open items are still on the list.
    db.prepare("INSERT INTO runs(id,started_at,finished_at,status,mode,metrics_json) VALUES('run-2',?,?,'COMPLETED','live','{}')").run(at(1440), at(1443));
    const next = buildDigest(db, config);
    assert.match(next.markdown, /Run \`run-2\`/); assert.deepEqual([next.counts.readyToSubmit, next.counts.manualApply], [1, 2]);

    // Closing everything (what "applied", "review --submit" and answering do) empties the list, and the run stops being notable.
    db.prepare("UPDATE jobs SET status='SUBMITTED'").run(); db.prepare("UPDATE applications SET status='SUBMITTED'").run(); db.prepare("UPDATE questions SET status='ANSWERED'").run();
    const done = buildDigest(db, config);
    assert.equal(done.notable, false); assert.deepEqual([done.counts.readyToSubmit, done.counts.manualApply, done.counts.needsInput], [0, 0, 0]);
  } finally { db.close(); await rm(root, { recursive: true, force: true }); }
});
