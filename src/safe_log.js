export function redactCredential(value) {
  return String(value || '').replace(/aep_[A-Za-z0-9]+_[A-Za-z0-9_-]+/g, '[credential]');
}

export function safeErrorCode(error) {
  const message = error?.code || error?.message || 'handler_error';
  const normalized = String(message).toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_+|_+$/g, '');
  return normalized.slice(0, 80) || 'handler_error';
}

export function safeLog(...args) {
  console.log(...args.map(redactCredential));
}
