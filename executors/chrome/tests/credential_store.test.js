import test from 'node:test';
import assert from 'node:assert/strict';
import { CredentialStore } from '../src/credential_store.js';
import { createChromeMock } from './helpers.js';

test('credential store gets sets and clears credential', async () => {
  const chrome = createChromeMock();
  const store = new CredentialStore(chrome);
  assert.equal(await store.getCredential(), '');
  await store.setCredential('aep_prefix_secret');
  assert.equal(await store.getCredential(), 'aep_prefix_secret');
  await store.clearCredential();
  assert.equal(await store.getCredential(), '');
});

test('credential store rejects empty credential', async () => {
  const store = new CredentialStore(createChromeMock());
  await assert.rejects(() => store.setCredential(''), /credential_required/);
});
