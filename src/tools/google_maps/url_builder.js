const GOOGLE_MAPS_SEARCH_BASE = 'https://www.google.com/maps/search/';

export function buildGoogleMapsSearchUrl(query, { locale = 'pt-BR' } = {}) {
  const url = new URL(GOOGLE_MAPS_SEARCH_BASE);
  url.searchParams.set('api', '1');
  url.searchParams.set('query', String(query || '').trim());
  if (locale) url.searchParams.set('hl', String(locale).slice(0, 16));
  return url.toString();
}

export function isAllowedGoogleMapsUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === 'www.google.com' && url.pathname.startsWith('/maps');
  } catch {
    return false;
  }
}
