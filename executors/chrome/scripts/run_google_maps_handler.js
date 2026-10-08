import { GoogleMapsSearchHandler } from '../src/tools/google_maps/handler.js';
import { buildGoogleMapsSearchUrl } from '../src/tools/google_maps/url_builder.js';

const query = process.argv.slice(2).join(' ').trim() || 'acougue itapevi sp';
const url = buildGoogleMapsSearchUrl(query, { locale: 'pt-BR' });
const logs = [];
const controller = {
  async withDedicatedTab(callback) {
    return callback({
      async search(searchQuery, options) {
        logs.push({ stage: 'manual_search', queryLength: searchQuery.length, maxResults: options.maxResults });
        return { found: 1, businesses: [{
          name: 'Açougue Manual',
          category: 'Açougue',
          address: 'Itapevi, SP',
          phone: null,
          website: null,
          maps_url: 'https://www.google.com/maps/place/Acougue+Manual/?cid=123',
          external_id: 'cid:123',
          source_query: searchQuery,
        }] };
      },
    });
  },
};

const handler = new GoogleMapsSearchHandler({
  controller,
  now: (() => { let t = 1000; return () => (t += 50); })(),
  logger: { info(message, metadata) { logs.push({ message, metadata }); }, log(message, metadata) { logs.push({ message, metadata }); } },
});

const result = await handler.handle({
  input: {
    schema_version: 1,
    queries: [query],
    target_region: 'Itapevi, SP',
    max_results: 5,
    locale: 'pt-BR',
  },
}, { id: 'manual-local' });

console.log(JSON.stringify({ query, url, logs, result }, null, 2));
