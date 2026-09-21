export class ExternalSearchProbeHandler {
  async handle(requestPayload) {
    return {
      executor: 'chrome',
      probe: 'ok',
      received: sanitizePayload(requestPayload),
      timestamp: new Date().toISOString(),
    };
  }
}

export function sanitizePayload(value) {
  if (Array.isArray(value)) return value.map(sanitizePayload);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, isSensitiveKey(key) ? '[redacted]' : sanitizePayload(item)]));
  }
  if (typeof value === 'string' && value.length > 2000) return `${value.slice(0, 2000)}...[truncated]`;
  return value;
}

function isSensitiveKey(key) {
  return ['password', 'token', 'secret', 'api_key', 'apikey', 'authorization'].some((fragment) => String(key).toLowerCase().includes(fragment));
}
