import test from 'node:test';
import assert from 'node:assert/strict';
import { ToolDispatcher } from '../src/tool_dispatcher.js';
import { ToolSlugs } from '../src/constants.js';

test('external search probe succeeds without external search', async () => {
  const dispatcher = new ToolDispatcher();
  const result = await dispatcher.execute({
    tool_definition: { slug: ToolSlugs.EXTERNAL_SEARCH_PROBE },
    request_payload: { query: 'hospitais', api_key: 'secret' },
  });
  assert.equal(result.executor, 'chrome');
  assert.equal(result.probe, 'ok');
  assert.equal(result.received.api_key, '[redacted]');
});

test('unknown tool fails controlled without dynamic execution', async () => {
  const dispatcher = new ToolDispatcher();
  await assert.rejects(() => dispatcher.execute({ tool_definition: { slug: 'unknown.tool' }, request_payload: {} }), (error) => error.code === 'unsupported_tool');
});

test('probe failure propagates as handler error', async () => {
  const dispatcher = new ToolDispatcher({ 'prospecting.external_search_probe': { async handle() { throw new Error('boom'); } } });
  await assert.rejects(() => dispatcher.execute({ tool_definition: { slug: 'prospecting.external_search_probe' }, request_payload: {} }), /boom/);
});
