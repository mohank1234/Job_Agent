import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadConfig } from '../src/config.js';
import { linkedinSearchUrls } from '../src/sources/linkedin.js';

const config = await loadConfig(process.cwd());

test('the small recent search uses only the roles it is given, every location, and the shortened age window', () => {
  const recent = structuredClone(config);
  recent.search.max_age_hours = 4;
  const urls = linkedinSearchUrls(recent, 16, ['QA Engineer', 'SDET']).map((url) => new URL(url));
  assert.ok(urls.length <= 16);
  assert.deepEqual([...new Set(urls.map((url) => url.searchParams.get('keywords')))], ['QA Engineer', 'SDET']);
  assert.ok(urls.every((url) => url.searchParams.get('f_TPR') === 'r14400'), 'only jobs posted in the last 4 hours');
  assert.ok(urls.every((url) => url.searchParams.get('sortBy') === 'DD'), 'newest first');
  assert.ok(urls.some((url) => url.searchParams.get('f_WT') === '2'), 'remote jobs are searched');
  assert.ok(urls.some((url) => url.searchParams.get('location') === 'Hyderabad'), 'on-site and hybrid jobs in the home city are searched');
});
