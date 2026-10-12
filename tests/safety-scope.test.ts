import assert from 'node:assert/strict';
import { test } from 'node:test';
import { detectSecurityText, securityScope } from '../src/safety.js';

const longPage = 'Provenir is the Decision Intelligence Platform. '.repeat(80) + 'Our identity verification and unusual activity detection products help banks.';

test('a LinkedIn company page that talks about identity verification is not a security stop', () => {
  const scope = securityScope('https://www.linkedin.com/company/provenir-global/', longPage, 'Provenir | Overview | Page posts');
  assert.equal(detectSecurityText(scope), undefined);
});

test('a LinkedIn dialog, a short stand-alone page, or a checkpoint route still stops', () => {
  assert.ok(detectSecurityText(securityScope('https://www.linkedin.com/company/x/', longPage, 'Security verification | We need to verify it is you')));
  assert.ok(detectSecurityText(securityScope('https://www.linkedin.com/feed/', 'Identity verification required to continue', '')));
  assert.ok(detectSecurityText(securityScope('https://www.linkedin.com/checkpoint/challenge/abc', longPage, '')));
});

test('outside LinkedIn the whole page is still checked', () => {
  assert.ok(detectSecurityText(securityScope('https://boards.greenhouse.io/acme/jobs/1', longPage, '')));
});
