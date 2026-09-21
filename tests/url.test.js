import test from 'node:test';
import assert from 'node:assert/strict';
import { joinUrl, normalizeBaseUrl, originPermissionPattern } from '../src/url.js';

test('normalizes http and https base URLs', () => {
  assert.equal(normalizeBaseUrl(' http://127.0.0.1:8000/ '), 'http://127.0.0.1:8000');
  assert.equal(normalizeBaseUrl('https://agents.example.com/path///?x=1#hash'), 'https://agents.example.com/path');
  assert.equal(joinUrl('https://agents.example.com/', '/api/v1/executors/me/'), 'https://agents.example.com/api/v1/executors/me/');
  assert.equal(originPermissionPattern('https://agents.example.com/path'), 'https://agents.example.com/*');
});

test('rejects unsupported platform URL schemes', () => {
  assert.throws(() => normalizeBaseUrl('javascript:alert(1)'), /unsupported_scheme/);
  assert.throws(() => normalizeBaseUrl('file:///tmp/a'), /unsupported_scheme/);
  assert.throws(() => normalizeBaseUrl('not a url'), /invalid/);
});
