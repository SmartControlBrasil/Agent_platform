export function formatCapabilities(capabilities = []) {
  if (!Array.isArray(capabilities) || capabilities.length === 0) return '-';
  return capabilities
    .map((capability) => {
      if (typeof capability === 'string') return capability;
      return capability?.slug || capability?.name || '';
    })
    .filter(Boolean)
    .join(', ') || '-';
}
