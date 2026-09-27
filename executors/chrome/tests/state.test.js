import test from 'node:test';
import assert from 'node:assert/strict';
import { ExecutorState } from '../src/constants.js';
import { deriveState, getState, setState } from '../src/state.js';
import { createChromeMock } from './helpers.js';

test('state machine derives operational states', () => {
  assert.equal(deriveState({ platformBaseUrl: '' }), ExecutorState.UNCONFIGURED);
  assert.equal(deriveState({ platformBaseUrl: 'https://a.test', pairing: { id: 'p' } }), ExecutorState.PAIRING);
  assert.equal(deriveState({ platformBaseUrl: 'https://a.test', executor: { id: 'e' }, lastHeartbeatAt: '' }), ExecutorState.PAIRED);
  assert.equal(deriveState({ platformBaseUrl: 'https://a.test', executor: { id: 'e' }, lastHeartbeatAt: 'now' }), ExecutorState.ONLINE);
});

test('persists normalized state in chrome storage', async () => {
  const chrome = createChromeMock();
  await setState({ platformBaseUrl: 'http://127.0.0.1:8000/', requestedName: '  Desk  ' }, chrome);
  const state = await getState(chrome);
  assert.equal(state.platformBaseUrl, 'http://127.0.0.1:8000');
  assert.equal(state.requestedName, 'Desk');
  assert.equal(state.state, ExecutorState.UNPAIRED);
});
