(() => {
  const phoneButtonSelectors = [
    'button[data-item-id^="phone:"]',
    'button[data-item-id^="phone"]',
    'button[aria-label*="Phone"]',
    'button[aria-label*="Telefone"]',
    'button[data-tooltip*="Phone"]',
    'button[data-tooltip*="Telefone"]',
  ].join(', ');
  const addressButtonSelectors = [
    'button[data-item-id="address"]',
    'button[data-item-id^="address:"]',
    'button[aria-label*="Address"]',
    'button[aria-label*="Endereço"]',
    'button[aria-label*="Endereco"]',
  ].join(', ');
  const websiteLinks = 'a[data-item-id="authority"], a[aria-label^="Website"], a[href^="http"]';

  function normalizeText(value) {
    return typeof value === 'string' ? value.trim().replace(/\s+/g, ' ') : '';
  }

  function stripLabelPrefix(text, labels) {
    const cleaned = normalizeText(text);
    if (!cleaned) return '';
    return cleaned.replace(new RegExp(`^(${labels.join('|')})\\s*:?\\s*`, 'i'), '').trim() || cleaned;
  }

  function extractPhone(root) {
    const button = root?.querySelector?.(phoneButtonSelectors);
    if (!button) return null;
    let raw = normalizeText(button.textContent || '');
    const aria = stripLabelPrefix(button.getAttribute?.('aria-label') || '', ['Phone', 'Telefone', 'Copiar número', 'Copy phone number']);
    if (!raw || /^(phone|telefone)$/i.test(raw)) raw = aria;
    if (!raw) {
      const dataId = button.getAttribute?.('data-item-id') || '';
      const telMatch = dataId.match(/phone:tel:([^;]+)/i) || dataId.match(/tel:([^;]+)/i);
      if (telMatch) raw = decodeURIComponent(telMatch[1]);
    }
    raw = stripLabelPrefix(raw, ['Phone', 'Telefone']);
    return raw || null;
  }

  function extractAddress(root) {
    const button = root?.querySelector?.(addressButtonSelectors);
    if (button) {
      const fromAria = stripLabelPrefix(button.getAttribute?.('aria-label') || '', ['Address', 'Endereço', 'Endereco', 'Copiar endereço', 'Copy address']);
      if (fromAria && !/^(address|endereço|endereco)$/i.test(fromAria)) return fromAria;
      const fromText = stripLabelPrefix(button.textContent || '', ['Address', 'Endereço', 'Endereco']);
      if (fromText && !/^(address|endereço|endereco)$/i.test(fromText)) return fromText;
    }
    return extractLabeledText(root, ['Address', 'Endereço', 'Endereco']);
  }

  function extractLabeledText(root, labels) {
    const candidates = Array.from(root?.querySelectorAll?.('[aria-label], button,div,span') || []);
    for (const candidate of candidates) {
      const text = normalizeText(candidate.getAttribute?.('aria-label') || candidate.textContent || '');
      const matched = labels.some((label) => text.toLowerCase().includes(label.toLowerCase()));
      if (matched) {
        const stripped = stripLabelPrefix(text, labels);
        if (stripped && !labels.some((label) => stripped.toLowerCase() === label.toLowerCase())) return stripped;
      }
    }
    return null;
  }

  function extractWebsite(root) {
    const links = Array.from(root?.querySelectorAll?.(websiteLinks) || []);
    const link = links.find((item) => normalizeUrl(item.href) && !normalizeMapsUrl(item.href));
    return normalizeUrl(link?.href);
  }

  function normalizeUrl(value) {
    const text = normalizeText(value);
    if (!text) return null;
    try {
      const url = new URL(text);
      if (!['http:', 'https:'].includes(url.protocol)) return null;
      url.hash = '';
      url.hostname = url.hostname.toLowerCase();
      return url.toString();
    } catch {
      return null;
    }
  }

  function normalizeMapsUrl(value) {
    const url = normalizeUrl(value);
    if (!url) return null;
    try {
      const parsed = new URL(url);
      return parsed.hostname.endsWith('google.com') && parsed.pathname.startsWith('/maps') ? parsed.toString() : null;
    } catch {
      return null;
    }
  }

  globalThis.__agentExecutorGoogleMapsDomExtraction = {
    extractPhone,
    extractAddress,
    extractWebsite,
    extractLabeledText,
    normalizeText,
  };
})();
