import test from 'node:test';
import assert from 'node:assert/strict';
import { ExecutorState } from '../src/constants.js';
import { getState, setState } from '../src/state.js';
import { consumeApprovedPairing, pollPairing, startPairing } from '../src/pairing_flow.js';
import { CredentialStore } from '../src/credential_store.js';
import { createChromeMock, createFetchMock, jsonResponse } from './helpers.js';

test('pairing lifecycle stores code then consumes credential once', async () => {
  const chrome = createChromeMock();
  await setState({ platformBaseUrl: 'https://agents.example.com', requestedName: 'Desk' }, chrome);
  const fetchImpl = createFetchMock((url) => {
    if (url.endsWith('/request/')) return jsonResponse({ id: 'pair-1', pairing_code: 'ABCD1234', expires_at: '2030-01-01T00:00:00Z', status: 'PENDING' }, 201);
    if (url.endsWith('/status/')) return jsonResponse({ id: 'pair-1', status: 'APPROVED' });
    if (url.endsWith('/consume/')) return jsonResponse({ credential: 'aep_prefix_secret', executor: { id: 'exec-1', name: 'Desk' } });
    throw new Error('unexpected_url');
  });

  await startPairing({ chromeApi: chrome, fetchImpl });
  assert.equal((await getState(chrome)).state, ExecutorState.PAIRING);
  await pollPairing({ chromeApi: chrome, fetchImpl });
  assert.equal(await new CredentialStore(chrome).getCredential(), 'aep_prefix_secret');
  assert.equal((await getState(chrome)).state, ExecutorState.PAIRED);
});

test('consume approved pairing refuses local double consume guard', async () => {
  const chrome = createChromeMock();
  await setState({ platformBaseUrl: 'https://agents.example.com', pairing: { id: 'p', pairingCode: 'CODE', consumed: true }, state: ExecutorState.PAIRING }, chrome);
  await assert.rejects(() => consumeApprovedPairing({ chromeApi: chrome, fetchImpl: async () => jsonResponse({}) }), /already_consumed/);
});
