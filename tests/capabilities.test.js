import test from 'node:test';
import assert from 'node:assert/strict';
import { formatCapabilities } from '../src/capabilities.js';

test('formats capability payloads from backend for popup display', () => {
  assert.equal(formatCapabilities([{ slug: 'prospecting.external_search_probe', name: 'External Search Probe' }]), 'prospecting.external_search_probe');
  assert.equal(formatCapabilities(['legacy.capability']), 'legacy.capability');
  assert.equal(formatCapabilities([]), '-');
});
