export function requireObject(value, code = 'invalid_response') {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(code);
  return value;
}

export function normalizeExecution(raw) {
  const value = requireObject(raw, 'invalid_execution');
  const tool = requireObject(value.tool_definition, 'invalid_tool_definition');
  if (!value.id || !tool.slug || !value.status) throw new Error('invalid_execution_shape');
  return {
    id: String(value.id),
    status: String(value.status),
    request_payload: requireObject(value.request_payload || {}, 'invalid_request_payload'),
    tool_definition: { slug: String(tool.slug) },
  };
}

export function normalizeQueueResponse(raw) {
  const value = requireObject(raw, 'invalid_queue_response');
  if (!Array.isArray(value.results)) throw new Error('invalid_queue_results');
  return value.results.map(normalizeExecution);
}
