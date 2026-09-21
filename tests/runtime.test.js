import test from 'node:test';
import assert from 'node:assert/strict';
import { ExecutorState } from '../src/constants.js';
import { CredentialStore } from '../src/credential_store.js';
import { heartbeat, pollAndRunJobs } from '../src/executor_runtime.js';
import { getState, setState } from '../src/state.js';
import { createChromeMock, createFetchMock, jsonResponse } from './helpers.js';

function execution() {
  return { id: 'exec-1', status: 'DISPATCHED', request_payload: { query: 'x' }, tool_definition: { slug: 'prospecting.external_search_probe' } };
}

test('heartbeat records online state', async () => {
  const chrome = createChromeMock();
  await setState({ platformBaseUrl: 'https://agents.example.com' }, chrome);
  await new CredentialStore(chrome).setCredential('aep_prefix_secret');
  const fetchImpl = createFetchMock(() => jsonResponse({ executor: { id: 'exec', name: 'Desk' } }));
  await heartbeat({ state: await getState(chrome), chromeApi: chrome, fetchImpl });
  const state = await getState(chrome);
  assert.equal(state.state, ExecutorState.ONLINE);
  assert.equal(state.executor.name, 'Desk');
});

test('queue parsing claim complete flow handles execution', async () => {
  const chrome = createChromeMock();
  await setState({ platformBaseUrl: 'https://agents.example.com' }, chrome);
  await new CredentialStore(chrome).setCredential('aep_prefix_secret');
  const fetchImpl = createFetchMock((url) => {
    if (url.endsWith('/tool-executions/')) return jsonResponse({ results: [execution()] });
    if (url.endsWith('/claim/')) return jsonResponse({ status: 'RUNNING' });
    if (url.endsWith('/complete/')) return jsonResponse({ status: 'SUCCEEDED' });
    throw new Error(`unexpected ${url}`);
  });
  const handled = await pollAndRunJobs({ state: await getState(chrome), chromeApi: chrome, fetchImpl });
  assert.deepEqual(handled, [{ id: 'exec-1', status: 'completed' }]);
  assert.equal((await getState(chrome)).lastExecutionStatus, 'SUCCEEDED');
});

test('handler failure calls fail endpoint with safe code', async () => {
  const chrome = createChromeMock();
  await setState({ platformBaseUrl: 'https://agents.example.com' }, chrome);
  await new CredentialStore(chrome).setCredential('aep_prefix_secret');
  const fetchImpl = createFetchMock((url) => {
    if (url.endsWith('/tool-executions/')) return jsonResponse({ results: [execution()] });
    if (url.endsWith('/claim/')) return jsonResponse({ status: 'RUNNING' });
    if (url.endsWith('/fail/')) return jsonResponse({ status: 'FAILED' });
    throw new Error(`unexpected ${url}`);
  });
  const dispatcher = { async execute() { throw new Error('token leaked in handler'); } };
  const handled = await pollAndRunJobs({ state: await getState(chrome), chromeApi: chrome, fetchImpl, dispatcher });
  assert.equal(handled[0].status, 'failed');
  const failCall = fetchImpl.calls.find((call) => call.url.endsWith('/fail/'));
  assert.match(failCall.init.body, /token_leaked_in_handler/);
  assert.doesNotMatch(failCall.init.body, /aep_prefix_secret/);
});

test('401 handling marks credential revoked without clearing it', async () => {
  const chrome = createChromeMock();
  await setState({ platformBaseUrl: 'https://agents.example.com' }, chrome);
  await new CredentialStore(chrome).setCredential('aep_prefix_secret');
  const fetchImpl = createFetchMock(() => jsonResponse({ error: 'executor_authentication_failed' }, 401));
  await heartbeat({ state: await getState(chrome), chromeApi: chrome, fetchImpl });
  assert.equal((await getState(chrome)).state, ExecutorState.REVOKED);
  assert.equal(await new CredentialStore(chrome).getCredential(), 'aep_prefix_secret');
});
