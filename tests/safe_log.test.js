import test from 'node:test';
import assert from 'node:assert/strict';
import { redactCredential, safeErrorCode } from '../src/safe_log.js';

test('credential is redacted from loggable strings', () => {
  assert.equal(redactCredential('credential aep_abc123_secret-value here'), 'credential [credential] here');
});

test('safe error code normalizes without stack trace', () => {
  assert.equal(safeErrorCode(new Error('Handler exploded badly!')), 'handler_exploded_badly');
});
