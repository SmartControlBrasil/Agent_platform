/** Safe, local-only diagnostics for Google Maps execution (no HTML/cookies/credentials). */

export function sanitizeMapsUrl(url) {
  try {
    const parsed = new URL(String(url || ''));
    if (parsed.protocol !== 'https:') return 'non_https';
    if (!parsed.hostname.endsWith('google.com')) return 'non_google_host';
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return 'invalid_url';
  }
}

export function createMapsDiagnostics(logger = null) {
  const logFn = logger?.info || logger?.log || null;
  return {
    stage(stage, metadata = {}) {
      if (!logFn) return;
      const payload = {
        stage: String(stage || '').slice(0, 80),
        ...metadata,
      };
      if (payload.url) payload.url = sanitizeMapsUrl(payload.url);
      logFn.call(logger, 'google_maps.diagnostics', payload);
    },
  };
}
