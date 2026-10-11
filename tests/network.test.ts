import assert from 'node:assert/strict';
import { test } from 'node:test';
import { assertOnline, describeError, OFFLINE_ERROR } from '../src/network.js';

const dnsError = (host: string, code = 'ENOTFOUND') => Object.assign(new Error(`getaddrinfo ${code} ${host}`), { code, hostname: host });

test('a run is refused when every probe host fails DNS, and nothing else trips it', async () => {
  await assert.rejects(assertOnline(async host => { throw dnsError(host); }), /No internet connection: DNS lookup failed.*ENOTFOUND www\.linkedin\.com.*Nothing was changed/);
  await assert.rejects(assertOnline(async host => { throw dnsError(host, 'EAI_AGAIN'); }), /No internet connection/);
  // One working host means the network is up; a non-DNS failure is not proof of being offline.
  await assertOnline(async host => { if (host === 'www.linkedin.com') throw dnsError(host); return { address: '1.2.3.4' }; });
  await assertOnline(async () => { throw new Error('something unrelated'); });
});

test('"fetch failed" carries its real cause so the digest shows why a source failed', () => {
  const offline = new TypeError('fetch failed', { cause: dnsError('boards-api.greenhouse.io') });
  assert.equal(describeError(offline), 'fetch failed (ENOTFOUND boards-api.greenhouse.io)');
  assert.ok(OFFLINE_ERROR.test(describeError(offline)));
  assert.equal(describeError(new TypeError('fetch failed', { cause: new Error('other side closed') })), 'fetch failed (other side closed)');
  assert.equal(describeError(new Error('HTTP 500: Server Error')), 'HTTP 500: Server Error');
  assert.equal(describeError('plain'), 'plain');
  // Chromium's DNS failure on the LinkedIn source is recognised as offline too.
  assert.ok(OFFLINE_ERROR.test('page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.linkedin.com/jobs/search/'));
  assert.ok(!OFFLINE_ERROR.test('No configured job cards found'));
});
