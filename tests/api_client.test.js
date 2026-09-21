import test from 'node:test';
import assert from 'node:assert/strict';
import { AgentPlatformClient, AgentPlatformError } from '../src/api_client.js';
import { createFetchMock, jsonResponse } from './helpers.js';

test('api client sends AgentExecutor auth header without executor id', async () => {
  const fetchImpl = createFetchMock(() => jsonResponse({ executor: { id: 'e' } }));
  const client = new AgentPlatformClient({ baseUrl: 'https://agents.example.com', credential: 'aep_prefix_secret', fetchImpl });
  await client.getExecutorMe();
  assert.equal(fetchImpl.calls[0].init.headers.Authorization, 'AgentExecutor aep_prefix_secret');
  assert.equal(fetchImpl.calls[0].init.body, undefined);
});

test('api client builds pairing and tool execution endpoints', async () => {
  const fetchImpl = createFetchMock(() => jsonResponse({ ok: true }));
  const client = new AgentPlatformClient({ baseUrl: 'http://127.0.0.1:8000/', credential: 'aep_prefix_secret', fetchImpl });
  await client.requestPairing({ requestedName: 'Chrome Executor' });
  await client.claimToolExecution('exec-1');
  await client.completeToolExecution('exec-1', { ok: true });
  assert.equal(fetchImpl.calls[0].url, 'http://127.0.0.1:8000/api/v1/executors/pairing/request/');
  assert.match(fetchImpl.calls[1].url, /claim\/$/);
  assert.match(fetchImpl.calls[2].url, /complete\/$/);
});

test('api client maps auth failure and invalid json', async () => {
  const authFetch = createFetchMock(() => jsonResponse({ error: 'executor_authentication_failed' }, 401));
  const client = new AgentPlatformClient({ baseUrl: 'https://agents.example.com', credential: 'bad', fetchImpl: authFetch });
  await assert.rejects(() => client.getExecutorMe(), (error) => error instanceof AgentPlatformError && error.status === 401);

  const invalidFetch = createFetchMock(() => ({ ok: true, status: 200, async text() { return 'nope'; } }));
  const invalidClient = new AgentPlatformClient({ baseUrl: 'https://agents.example.com', fetchImpl: invalidFetch });
  await assert.rejects(() => invalidClient.requestPairing({ requestedName: 'x' }), /invalid_json_response/);
});
