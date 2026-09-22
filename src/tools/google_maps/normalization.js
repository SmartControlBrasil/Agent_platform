export function normalizeBusiness(raw = {}, sourceQuery = '') {
  const business = {
    name: normalizeText(raw.name),
    category: normalizeNullableText(raw.category),
    address: normalizeNullableText(raw.address),
    phone: normalizeNullableText(raw.phone),
    website: normalizeNullableUrl(raw.website),
    maps_url: normalizeNullableMapsUrl(raw.maps_url),
    external_id: normalizeNullableText(raw.external_id) || extractExternalId(raw.maps_url),
    source_query: normalizeNullableText(raw.source_query) || normalizeText(sourceQuery),
  };
  if (!business.name) return null;
  if (!business.maps_url && !business.external_id) return null;
  return business;
}

export function normalizeText(value) {
  if (typeof value !== 'string') return '';
  return value.trim().replace(/\s+/g, ' ');
}

export function normalizeNullableText(value) {
  const text = normalizeText(value);
  return text || null;
}

export function normalizeNullableUrl(value) {
  const text = normalizeText(value);
  if (!text) return null;
  try {
    const url = new URL(text);
    if (!['http:', 'https:'].includes(url.protocol)) return null;
    url.hash = '';
    url.protocol = url.protocol.toLowerCase();
    url.hostname = url.hostname.toLowerCase();
    return url.toString();
  } catch {
    return null;
  }
}

export function normalizeNullableMapsUrl(value) {
  const url = normalizeNullableUrl(value);
  if (!url) return null;
  try {
    const parsed = new URL(url);
    if (!parsed.hostname.endsWith('google.com') || !parsed.pathname.startsWith('/maps')) return null;
    return parsed.toString();
  } catch {
    return null;
  }
}

export function extractExternalId(mapsUrl = '') {
  const text = normalizeText(mapsUrl);
  if (!text) return null;
  try {
    const url = new URL(text);
    const cid = url.searchParams.get('cid');
    if (cid) return `cid:${cid}`;
    const match = decodeURIComponent(url.href).match(/!1s([^!]+)/);
    return match?.[1] || null;
  } catch {
    const match = text.match(/[?&]cid=([0-9]+)/);
    return match ? `cid:${match[1]}` : null;
  }
}
