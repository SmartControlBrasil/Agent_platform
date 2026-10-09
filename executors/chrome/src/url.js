export function normalizeBaseUrl(rawUrl) {
  const value = String(rawUrl || '').trim();
  if (!value) {
    throw new Error('platform_url_required');
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch (error) {
    throw new Error('platform_url_invalid');
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('platform_url_unsupported_scheme');
  }
  parsed.hash = '';
  parsed.search = '';
  parsed.pathname = parsed.pathname.replace(/\/+$/, '');
  return parsed.toString().replace(/\/$/, '');
}

export function joinUrl(baseUrl, path) {
  return `${normalizeBaseUrl(baseUrl)}${path.startsWith('/') ? path : `/${path}`}`;
}

export function platformPanelUrl(baseUrl) {
  return joinUrl(baseUrl, '/painel/');
}

export function originPermissionPattern(baseUrl) {
  const parsed = new URL(normalizeBaseUrl(baseUrl));
  return `${parsed.origin}/*`;
}
