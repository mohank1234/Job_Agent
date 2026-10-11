import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ageBatch, compareByPriority, locationTier } from '../src/priority.js';

const now = Date.parse('2026-10-11T12:00:00Z');
const ago = (hours: number): string => new Date(now - hours * 3_600_000).toISOString();

test('jobs fall into the six age batches, oldest bucket last', () => {
  assert.deepEqual([0.5, 1, 2, 5, 11, 20, 30].map((hours) => ageBatch(ago(hours), now)), ['0-1h', '0-1h', '1-3h', '3-6h', '6-12h', '12-24h', 'older than 24h']);
  assert.equal(ageBatch(undefined, now), 'unknown age');
});

test('Hyderabad, then Remote, then Bangalore, then every other location', () => {
  assert.deepEqual([['Hyderabad, Telangana, India', ''], ['Secunderabad', 'Hybrid'], ['India', 'Remote'], ['Bengaluru, Karnataka, India', 'On-site'], ['Bangalore', ''], ['Pune, Maharashtra', '']].map(([l, m]) => locationTier(l, m)), [1, 1, 2, 3, 3, 4]);
  assert.equal(locationTier('Remote - Hyderabad', ''), 1, 'a remote job in Hyderabad is a Hyderabad job');
});

test('the work order is age batch first, location second, fit third', () => {
  const jobs = [
    { id: 'pune-new', posted_at: ago(0.5), location: 'Pune', fit_score: 99 },
    { id: 'hyd-3h', posted_at: ago(2), location: 'Hyderabad', fit_score: 60 },
    { id: 'blr-new', posted_at: ago(0.2), location: 'Bengaluru', fit_score: 70 },
    { id: 'remote-new', posted_at: ago(0.4), location: 'India', work_mode: 'Remote', fit_score: 65 },
    { id: 'hyd-new-low', posted_at: ago(0.9), location: 'Hyderabad', fit_score: 61 },
    { id: 'hyd-new-high', posted_at: ago(0.1), location: 'Hyderabad', fit_score: 90 }
  ];
  assert.deepEqual([...jobs].sort(compareByPriority(undefined, now)).map((job) => job.id), ['hyd-new-high', 'hyd-new-low', 'remote-new', 'blr-new', 'pune-new', 'hyd-3h']);
});
