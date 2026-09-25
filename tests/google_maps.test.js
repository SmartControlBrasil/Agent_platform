import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { ToolDispatcher } from '../src/tool_dispatcher.js';
import { ToolSlugs } from '../src/constants.js';
import { validateGoogleMapsInput } from '../src/tools/google_maps/input_contract.js';
import { buildGoogleMapsSearchUrl, isAllowedGoogleMapsUrl } from '../src/tools/google_maps/url_builder.js';
import { normalizeBusiness, normalizeNullableMapsUrl, normalizeNullableUrl } from '../src/tools/google_maps/normalization.js';
import { businessKey, dedupeBusinesses, mergeBusiness } from '../src/tools/google_maps/dedupe.js';
import { GoogleMapsSearchHandler } from '../src/tools/google_maps/handler.js';
import { GoogleMapsBrowserController } from '../src/tools/google_maps/browser_controller.js';
import { classifyGoogleMapsFixture, detectChallengeHtml } from '../src/tools/google_maps/fixture_analysis.js';
import { validateGoogleMapsMessage, GoogleMapsMessages } from '../src/tools/google_maps/protocol.js';

function validPayload(overrides = {}) {
  return {
    input: {
      schema_version: 1,
      queries: [' hospital privado Barueri '],
      target_region: ' Barueri ',
      max_results: 10,
      ...overrides,
    },
  };
}

function business(overrides = {}) {
  return {
    name: 'Hospital A',
    category: 'Hospital',
    address: 'Rua A, 1',
    phone: null,
    website: null,
    maps_url: 'https://www.google.com/maps/place/Hospital+A/?cid=123',
    external_id: 'cid:123',
    source_query: 'hospital privado Barueri',
    ...overrides,
  };
}

test('google maps input validation normalizes backend contract', () => {
  const input = validateGoogleMapsInput(validPayload());
  assert.deepEqual(input.queries, ['hospital privado Barueri']);
  assert.equal(input.target_region, 'Barueri');
  assert.equal(input.locale, 'pt-BR');
  assert.equal(input.max_results, 10);
});

test('google maps input validation rejects invalid limits and URL queries', () => {
  const invalid = [
    validPayload({ schema_version: 2 }),
    validPayload({ queries: [] }),
    validPayload({ queries: Array.from({ length: 21 }, (_, index) => `q${index}`) }),
    validPayload({ queries: ['https://www.google.com/maps/search/foo'] }),
    validPayload({ max_results: 0 }),
    validPayload({ max_results: 101 }),
  ];
  for (const payload of invalid) {
    assert.throws(() => validateGoogleMapsInput(payload), /schema_version|queries|max_results/);
  }
});

test('google maps search URL builder uses encoded query and allowed host only', () => {
  const url = buildGoogleMapsSearchUrl('hospital privado Barueri', { locale: 'pt-BR' });
  assert.equal(new URL(url).searchParams.get('query'), 'hospital privado Barueri');
  assert.equal(new URL(url).searchParams.get('hl'), 'pt-BR');
  assert.equal(isAllowedGoogleMapsUrl(url), true);
  assert.equal(isAllowedGoogleMapsUrl('https://evil.example/maps/search/?query=x'), false);
});

test('normalization preserves phone and rejects invalid websites', () => {
  const normalized = normalizeBusiness({
    name: ' Hospital A ',
    phone: ' (11) 1234-5678 ',
    website: 'ftp://invalid.example',
    maps_url: 'HTTPS://www.google.com/maps/place/Hospital+A/?cid=123#frag',
  }, 'hospital privado Barueri');
  assert.equal(normalized.name, 'Hospital A');
  assert.equal(normalized.phone, '(11) 1234-5678');
  assert.equal(normalized.website, null);
  assert.equal(normalized.external_id, 'cid:123');
  assert.equal(normalizeNullableUrl('https://Example.com/path#frag'), 'https://example.com/path');
  assert.equal(normalizeNullableMapsUrl('https://example.com/maps/place/x'), null);
});

test('dedupe uses external_id then maps_url then name address and merges fields', () => {
  const byExternalId = dedupeBusinesses([business({ website: null }), business({ name: 'Hospital A Prime', website: 'https://a.example/' })]);
  assert.equal(byExternalId.businesses.length, 1);
  assert.equal(byExternalId.businesses[0].website, 'https://a.example/');
  assert.equal(byExternalId.duplicatesRemoved, 1);

  const byMapsUrl = dedupeBusinesses([
    business({ external_id: null, maps_url: 'https://www.google.com/maps/place/A/' }),
    business({ external_id: null, maps_url: 'https://www.google.com/maps/place/A/' }),
  ]);
  assert.equal(byMapsUrl.businesses.length, 1);

  const byNameAddress = dedupeBusinesses([
    business({ external_id: null, maps_url: null, name: 'Hospital A', address: 'Rua A' }),
    business({ external_id: null, maps_url: null, name: ' hospital a ', address: ' Rua A ' }),
  ]);
  assert.equal(byNameAddress.businesses.length, 1);
  assert.equal(businessKey(byNameAddress.businesses[0]), 'name_address:hospital a|rua a');
  assert.deepEqual(mergeBusiness({ name: 'A', phone: null }, { name: 'B', phone: '123' }), { name: 'A', phone: '123' });
});

test('handler orchestrates multi-query sequentially and respects global max_results', async () => {
  const searches = [];
  const controller = {
    async withDedicatedTab(callback) {
      return callback({
        async search(query, options) {
          searches.push({ query, options });
          return {
            found: 2,
            businesses: [
              business({ name: `${query} A`, external_id: `cid:${query}:a`, maps_url: `https://www.google.com/maps/place/A/?cid=${query.length}1`, source_query: query }),
              business({ name: `${query} B`, external_id: `cid:${query}:b`, maps_url: `https://www.google.com/maps/place/B/?cid=${query.length}2`, source_query: query }),
            ],
          };
        },
      });
    },
  };
  const handler = new GoogleMapsSearchHandler({ controller, now: (() => { let t = 1000; return () => (t += 50); })(), logger: null });
  const result = await handler.handle(validPayload({ queries: ['q1', 'q2', 'q3'], max_results: 3 }), { id: 'exec-1' });
  assert.equal(searches.length, 2);
  assert.equal(result.businesses.length, 3);
  assert.equal(result.stats.queries_requested, 3);
  assert.equal(result.stats.queries_executed, 2);
  assert.equal(result.stats.businesses_returned, 3);
  assert.equal(result.schema_version, 1);
});

test('handler deduplicates across queries and reports completed schema', async () => {
  const controller = {
    async withDedicatedTab(callback) {
      return callback({
        async search(query) {
          return { found: 1, businesses: [business({ source_query: query })] };
        },
      });
    },
  };
  const result = await new GoogleMapsSearchHandler({ controller, now: () => 1, logger: null }).handle(validPayload({ queries: ['a', 'b'], max_results: 10 }));
  assert.equal(result.status, 'completed');
  assert.equal(result.businesses.length, 1);
  assert.equal(result.stats.duplicates_removed, 1);
});

test('challenge, changed structure, and no-results fixtures classify without internet', () => {
  const challenge = readFileSync(new URL('./fixtures/google_maps_challenge.html', import.meta.url), 'utf8');
  const changed = readFileSync(new URL('./fixtures/google_maps_changed_structure.html', import.meta.url), 'utf8');
  const noResults = readFileSync(new URL('./fixtures/google_maps_no_results.html', import.meta.url), 'utf8');
  const feed = readFileSync(new URL('./fixtures/google_maps_feed.html', import.meta.url), 'utf8');
  assert.equal(detectChallengeHtml(challenge), true);
  assert.equal(classifyGoogleMapsFixture(challenge), 'challenge');
  assert.equal(classifyGoogleMapsFixture(changed), 'changed_structure');
  assert.equal(classifyGoogleMapsFixture(noResults), 'no_results');
  assert.equal(classifyGoogleMapsFixture(feed), 'feed');
});

test('message protocol rejects arbitrary commands', () => {
  assert.equal(validateGoogleMapsMessage({ type: GoogleMapsMessages.WAIT_READY }), true);
  assert.equal(validateGoogleMapsMessage({ type: 'RUN_REMOTE_CODE' }), false);
  assert.equal(validateGoogleMapsMessage(null), false);
});

test('tool dispatcher registers google maps and keeps unknown tool controlled', async () => {
  const dispatcher = new ToolDispatcher({
    [ToolSlugs.GOOGLE_MAPS_SEARCH]: { async handle() { return { schema_version: 1, status: 'completed', businesses: [], stats: {} }; } },
  });
  const result = await dispatcher.execute({ tool_definition: { slug: ToolSlugs.GOOGLE_MAPS_SEARCH }, request_payload: validPayload() });
  assert.equal(result.status, 'completed');
  await assert.rejects(() => new ToolDispatcher({}).execute({ tool_definition: { slug: 'unknown' }, request_payload: {} }), (error) => error.code === 'unsupported_tool');
});

test('content script is isolated from backend credential and network calls', () => {
  const source = readFileSync(new URL('../src/tools/google_maps/content_script.js', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /Authorization|AgentExecutor|credential|fetch\(/);
  assert.match(source, /unsupported_message/);
});


test('browser controller uses dedicated tab and closes it after success', async () => {
  const calls = [];
  const chrome = {
    tabs: {
      async create(options) { calls.push(['create', options]); return { id: 7 }; },
      async update(tabId, options) { calls.push(['update', tabId, options]); },
      async get(tabId) { calls.push(['get', tabId]); return { id: tabId, status: 'complete' }; },
      async sendMessage(_tabId, message) {
        calls.push(['send', message.type]);
        if (message.type === GoogleMapsMessages.WAIT_READY) return { ok: true, ready: true };
        if (message.type === GoogleMapsMessages.COLLECT_FEED) return { ok: true, businesses: [business()], endOfResults: true };
        if (message.type === GoogleMapsMessages.OPEN_RESULT) return { ok: true };
        if (message.type === GoogleMapsMessages.EXTRACT_DETAIL) return { ok: true, business: business({ website: 'https://hospital-a.example/' }) };
        return { ok: true };
      },
      async remove(tabId) { calls.push(['remove', tabId]); },
    },
    scripting: { async executeScript(args) { calls.push(['executeScript', args.target.tabId]); } },
  };
  const controller = new GoogleMapsBrowserController(chrome, { navigationTimeoutMs: 10, readyTimeoutMs: 10 });
  const result = await controller.withDedicatedTab((tab) => tab.search('hospital privado Barueri', { maxResults: 1 }));
  assert.equal(result.businesses.length, 1);
  assert.equal(result.businesses[0].website, 'https://hospital-a.example/');
  assert.deepEqual(calls[0][0], 'create');
  assert.deepEqual(calls.at(-1), ['remove', 7]);
});

test('browser controller maps challenge and changed structure to controlled errors', async () => {
  const baseChrome = (readyResponse) => ({
    tabs: {
      async create() { return { id: 1 }; },
      async update() {},
      async get() { return { id: 1, status: 'complete' }; },
      async sendMessage(_tabId, message) {
        if (message.type === GoogleMapsMessages.WAIT_READY) return readyResponse;
        return { ok: true, businesses: [] };
      },
      async remove() {},
    },
    scripting: { async executeScript() {} },
  });
  await assert.rejects(
    () => new GoogleMapsBrowserController(baseChrome({ ok: true, challenge: true })).withDedicatedTab((tab) => tab.search('q')),
    (error) => error.code === 'google_challenge',
  );
  await assert.rejects(
    () => new GoogleMapsBrowserController(baseChrome({ ok: false, code: 'page_structure_changed' })).withDedicatedTab((tab) => tab.search('q')),
    (error) => error.code === 'page_structure_changed',
  );
});

test('browser controller waits through transient empty feed and then returns businesses', async () => {
  let collectCalls = 0;
  const chrome = {
    tabs: {
      async create() { return { id: 10 }; },
      async update() {},
      async get() { return { id: 10, status: 'complete' }; },
      async sendMessage(_tabId, message) {
        if (message.type === GoogleMapsMessages.WAIT_READY) return { ok: true, ready: true };
        if (message.type === GoogleMapsMessages.COLLECT_FEED) {
          collectCalls += 1;
          if (collectCalls < 3) return { ok: true, businesses: [], endOfResults: false };
          return { ok: true, businesses: [business()], endOfResults: true };
        }
        return { ok: true };
      },
      async remove() {},
    },
    scripting: { async executeScript() {} },
  };
  const result = await new GoogleMapsBrowserController(chrome, { navigationTimeoutMs: 10, readyTimeoutMs: 10 })
    .withDedicatedTab((tab) => tab.search('q', { maxResults: 5 }));
  assert.equal(result.found, 1);
  assert.equal(result.businesses.length, 1);
});

test('browser controller emits controlled error when feed never yields recognizable cards', async () => {
  const chrome = {
    tabs: {
      async create() { return { id: 11 }; },
      async update() {},
      async get() { return { id: 11, status: 'complete' }; },
      async sendMessage(_tabId, message) {
        if (message.type === GoogleMapsMessages.WAIT_READY) return { ok: true, ready: true };
        if (message.type === GoogleMapsMessages.COLLECT_FEED) return { ok: true, businesses: [], endOfResults: false };
        return { ok: true };
      },
      async remove() {},
    },
    scripting: { async executeScript() {} },
  };
  await assert.rejects(
    () => new GoogleMapsBrowserController(chrome, { navigationTimeoutMs: 10, readyTimeoutMs: 10 })
      .withDedicatedTab((tab) => tab.search('q', { maxResults: 5 })),
    (error) => error.code === 'results_not_loaded',
  );
});


test('manifest grants only scoped google maps permissions and no global host access', () => {
  const manifest = JSON.parse(readFileSync(new URL('../manifest.json', import.meta.url), 'utf8'));
  assert.deepEqual(manifest.permissions.filter((permission) => ['tabs', 'scripting'].includes(permission)).sort(), ['scripting', 'tabs']);
  assert.ok(manifest.host_permissions.every((pattern) => pattern.startsWith('https://www.google.com/maps')));
  assert.equal(manifest.optional_host_permissions.includes('<all_urls>'), false);
  assert.equal(manifest.optional_host_permissions.includes('https://*/*'), false);
  assert.equal(manifest.optional_host_permissions.includes('http://*/*'), false);
});
