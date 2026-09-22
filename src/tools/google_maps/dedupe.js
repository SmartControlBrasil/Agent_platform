import { normalizeText } from './normalization.js';

export function dedupeBusinesses(items, maxResults = 100) {
  const byKey = new Map();
  let duplicatesRemoved = 0;
  for (const item of items) {
    if (!item) continue;
    const key = businessKey(item);
    if (!key) continue;
    if (byKey.has(key)) {
      duplicatesRemoved += 1;
      byKey.set(key, mergeBusiness(byKey.get(key), item));
      continue;
    }
    byKey.set(key, item);
  }
  return { businesses: Array.from(byKey.values()).slice(0, maxResults), duplicatesRemoved };
}

export function businessKey(item) {
  if (item.external_id) return `external_id:${item.external_id.toLowerCase()}`;
  if (item.maps_url) return `maps_url:${item.maps_url.toLowerCase()}`;
  const name = normalizeText(item.name).toLowerCase();
  const address = normalizeText(item.address).toLowerCase();
  return name && address ? `name_address:${name}|${address}` : '';
}

export function mergeBusiness(first, second) {
  return Object.fromEntries(Object.keys(first).map((key) => [key, first[key] ?? second[key] ?? null]));
}
