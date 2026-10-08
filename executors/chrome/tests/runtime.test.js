import test from 'node:test';
import assert from 'node:assert/strict';
import { ExecutorState } from '../src/constants.js';
import { CredentialStore } from '../src/credential_store.js';
import { heartbeat, pollAndRunJobs } from '../src/executor_runtime.js';
import { ToolDispatcher } from '../src/tool_dispatcher.js';
import { GoogleMapsToolError } from '../src/tools/google_maps/errors.js';
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
    if (url.endsWith('/progress/')) return jsonResponse({ status: 'RUNNING' });
    if (url.endsWith('/complete/')) return jsonResponse({ status: 'SUCCEEDED' });
    throw new Error(`unexpected ${url}`);
  });
  const handled = await pollAndRunJobs({ state: await getState(chrome), chromeApi: chrome, fetchImpl });
  assert.deepEqual(handled, [{ id: 'exec-1', status: 'completed' }]);
  assert.equal((await getState(chrome)).lastExecutionStatus, 'SUCCEEDED');
});

test('progress failure is non-fatal and emits safe warning', async () => {
  const chrome = createChromeMock();
  await setState({ platformBaseUrl: 'https://agents.example.com' }, chrome);
  await new CredentialStore(chrome).setCredential('aep_prefix_secret');
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args);
  try {
    const fetchImpl = createFetchMock((url) => {
      if (url.endsWith('/tool-executions/')) return jsonResponse({ results: [execution()] });
      if (url.endsWith('/claim/')) return jsonResponse({ status: 'RUNNING' });
      if (url.endsWith('/progress/')) return jsonResponse({ error: 'progress_rejected', message: 'credential aep_prefix_secret leaked' }, 500);
      if (url.endsWith('/complete/')) return jsonResponse({ status: 'SUCCEEDED' });
      throw new Error(`unexpected ${url}`);
    });

    const handled = await pollAndRunJobs({ state: await getState(chrome), chromeApi: chrome, fetchImpl });

    assert.deepEqual(handled, [{ id: 'exec-1', status: 'completed' }]);
    const warning = warnings.find((entry) => entry[0] === 'executor.progress_failed');
    assert.ok(warning);
    assert.equal(warning[1].execution_id, 'exec-1');
    assert.equal(warning[1].stage, 'executor_started');
    assert.equal(warning[1].error_code, 'progress_rejected');
    assert.equal(warning[1].http_status, 500);
    assert.equal(warning[1].message, 'progress_rejected');
    assert.doesNotMatch(JSON.stringify(warning[1]), /aep_prefix_secret|Authorization|cookie/i);
  } finally {
    console.warn = originalWarn;
  }
});

test('handler failure calls fail endpoint with safe code', async () => {
  const chrome = createChromeMock();
  await setState({ platformBaseUrl: 'https://agents.example.com' }, chrome);
  await new CredentialStore(chrome).setCredential('aep_prefix_secret');
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args);
  try {
    const fetchImpl = createFetchMock((url) => {
      if (url.endsWith('/tool-executions/')) return jsonResponse({ results: [execution()] });
      if (url.endsWith('/claim/')) return jsonResponse({ status: 'RUNNING' });
      if (url.endsWith('/progress/')) return jsonResponse({ status: 'RUNNING' });
      if (url.endsWith('/fail/')) return jsonResponse({ status: 'FAILED' });
      throw new Error(`unexpected ${url}`);
    });
    const dispatcher = { async execute() { throw new Error('token leaked in handler'); } };
    const handled = await pollAndRunJobs({ state: await getState(chrome), chromeApi: chrome, fetchImpl, dispatcher });

    assert.equal(handled[0].status, 'failed');
    const failCall = fetchImpl.calls.find((call) => call.url.endsWith('/fail/'));
    assert.match(failCall.init.body, /token_leaked_in_handler/);
    assert.doesNotMatch(failCall.init.body, /aep_prefix_secret/);
    const warning = warnings.find((entry) => entry[0] === 'executor.execution_failed');
    assert.ok(warning);
    assert.doesNotMatch(JSON.stringify(warning[1]), /aep_prefix_secret|Authorization|cookie/i);
  } finally {
    console.warn = originalWarn;
  }
});

test('GoogleMapsToolError diagnostics reach fail endpoint and safe failure warning', async () => {
  const chrome = createChromeMock();
  await setState({ platformBaseUrl: 'https://agents.example.com' }, chrome);
  await new CredentialStore(chrome).setCredential('aep_prefix_secret');
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args);
  try {
    const diagnostics = {
      stage: 'collect_feed_empty',
      current_query: 'hospitais Barueri',
      hasResultsFeed: false,
      articleCount: 4,
      placeAnchorCount: 0,
    };
    const fetchImpl = createFetchMock((url) => {
      if (url.endsWith('/tool-executions/')) return jsonResponse({ results: [execution()] });
      if (url.endsWith('/claim/')) return jsonResponse({ status: 'RUNNING' });
      if (url.endsWith('/fail/')) return jsonResponse({ status: 'FAILED' });
      throw new Error(`unexpected ${url}`);
    });
    const dispatcher = new ToolDispatcher({
      'prospecting.external_search_probe': {
        async handle() {
          throw new GoogleMapsToolError(
            'results_timeout',
            'Google Maps did not expose recognizable business cards in time.',
            diagnostics,
          );
        },
      },
    });

    const handled = await pollAndRunJobs({ state: await getState(chrome), chromeApi: chrome, fetchImpl, dispatcher });

    assert.equal(handled[0].status, 'failed');
    const failCall = fetchImpl.calls.find((call) => call.url.endsWith('/fail/'));
    const body = JSON.parse(failCall.init.body);
    assert.equal(body.error_code, 'results_timeout');
    assert.deepEqual(body.diagnostics, diagnostics);
    const warning = warnings.find((entry) => entry[0] === 'executor.execution_failed');
    assert.ok(warning);
    assert.equal(warning[1].has_diagnostics, true);
    assert.equal(warning[1].diagnostics_stage, 'collect_feed_empty');
    assert.equal(warning[1].diagnostics_feed_found, false);
    assert.equal(warning[1].diagnostics_article_count, 4);
    assert.equal(warning[1].diagnostics_place_anchor_count, 0);
    assert.doesNotMatch(JSON.stringify(warning[1]), /hospitais|aep_prefix_secret|Authorization|cookie/i);
  } finally {
    console.warn = originalWarn;
  }
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
