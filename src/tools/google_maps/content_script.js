(() => {
  const Messages = Object.freeze({
    WAIT_READY: 'GMAPS_WAIT_READY',
    COLLECT_FEED: 'GMAPS_COLLECT_FEED',
    OPEN_RESULT: 'GMAPS_OPEN_RESULT',
    EXTRACT_DETAIL: 'GMAPS_EXTRACT_DETAIL',
    SCROLL: 'GMAPS_SCROLL',
    DETECT_CHALLENGE: 'GMAPS_DETECT_CHALLENGE',
  });
  const allowedMessages = new Set(Object.values(Messages));
  const selectors = Object.freeze({
    feed: '[role="feed"], div[aria-label][role="main"]',
    resultAnchors: 'a[href*="/maps/place/"], a[href*="/maps/search/"]',
    placePanel: '[role="main"]',
    websiteLinks: 'a[data-item-id="authority"], a[aria-label^="Website"], a[href^="http"]',
    phoneButtons: 'button[data-item-id^="phone"], button[aria-label*="Phone"], button[aria-label*="Telefone"]',
    noResultsText: 'No results found,Nenhum resultado encontrado',
    challengeText: 'captcha,unusual traffic,not a robot,verifique se você não é um robô,tráfego incomum',
  });

  if (globalThis.__agentExecutorGoogleMapsContentScript) return;
  globalThis.__agentExecutorGoogleMapsContentScript = true;

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || typeof message !== 'object' || !allowedMessages.has(message.type)) {
      sendResponse({ ok: false, code: 'unsupported_message' });
      return false;
    }
    handleMessage(message).then(sendResponse).catch(() => sendResponse({ ok: false, code: 'handler_error' }));
    return true;
  });

  async function handleMessage(message) {
    if (message.type === Messages.DETECT_CHALLENGE) return { ok: true, challenge: detectChallenge(document) };
    if (message.type === Messages.WAIT_READY) return waitReady(message.timeoutMs || 20000);
    if (message.type === Messages.COLLECT_FEED) return collectFeed(document, message.sourceQuery || '', message.limit || 100);
    if (message.type === Messages.SCROLL) return scrollFeed(document, message.timeoutMs || 12000);
    if (message.type === Messages.EXTRACT_DETAIL) return { ok: true, business: extractDetailBusiness(document, message.sourceQuery || '') };
    if (message.type === Messages.OPEN_RESULT) return openResult(document, message.mapsUrl || '');
    return { ok: false, code: 'unsupported_message' };
  }

  async function waitReady(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (detectChallenge(document)) return { ok: true, ready: false, challenge: true };
      if (detectNoResults(document)) return { ok: true, ready: true, noResults: true };
      if (findFeed(document) || findResultAnchors(document).length > 0) return { ok: true, ready: true };
      await sleep(250);
    }
    return { ok: false, ready: false, code: 'page_structure_changed' };
  }

  function collectFeed(doc, sourceQuery, limit) {
    if (detectChallenge(doc)) return { ok: true, challenge: true, businesses: [] };
    const anchors = findResultAnchors(doc);
    const businesses = anchors.slice(0, limit).map((anchor) => businessFromAnchor(anchor, sourceQuery)).filter(Boolean);
    return { ok: true, businesses, endOfResults: detectEndOfResults(doc) || businesses.length >= limit };
  }

  async function scrollFeed(doc, timeoutMs) {
    const feed = findFeed(doc) || doc.scrollingElement || doc.documentElement;
    const before = feed.scrollTop || 0;
    feed.scrollTop = before + Math.max(feed.clientHeight || 800, 600);
    feed.dispatchEvent?.(new Event('scroll', { bubbles: true }));
    await sleep(Math.min(timeoutMs, 800));
    return { ok: true };
  }

  function businessFromAnchor(anchor, sourceQuery) {
    const mapsUrl = normalizeMapsUrl(anchor.href);
    const name = normalizeText(anchor.getAttribute('aria-label') || anchor.textContent || '');
    if (!mapsUrl || !name) return null;
    const container = anchor.closest('[role="article"], div') || anchor.parentElement;
    return {
      name,
      category: extractLabeledText(container, ['Category', 'Categoria']),
      address: extractLabeledText(container, ['Address', 'Endereço']),
      phone: extractPhone(container),
      website: extractWebsite(container),
      maps_url: mapsUrl,
      external_id: extractExternalId(mapsUrl),
      source_query: sourceQuery,
    };
  }

  async function openResult(doc, mapsUrl) {
    const normalized = normalizeMapsUrl(mapsUrl);
    if (!normalized) return { ok: false, code: 'handler_error' };
    const anchor = findResultAnchors(doc).find((item) => normalizeMapsUrl(item.href) === normalized);
    if (!anchor) return { ok: false, code: 'page_structure_changed' };
    anchor.click();
    await sleep(1000);
    return { ok: true };
  }

  function extractDetailBusiness(doc, sourceQuery) {
    const panel = doc.querySelector(selectors.placePanel) || doc.body;
    const heading = panel.querySelector('h1, [role="heading"]');
    const mapsUrl = normalizeMapsUrl(location.href);
    return {
      name: normalizeText(heading?.textContent || ''),
      category: extractLabeledText(panel, ['Category', 'Categoria']),
      address: extractLabeledText(panel, ['Address', 'Endereço']),
      phone: extractPhone(panel),
      website: extractWebsite(panel),
      maps_url: mapsUrl,
      external_id: extractExternalId(mapsUrl),
      source_query: sourceQuery,
    };
  }

  function findFeed(doc) { return doc.querySelector(selectors.feed); }
  function findResultAnchors(doc) { return Array.from(doc.querySelectorAll(selectors.resultAnchors)).filter((anchor) => normalizeMapsUrl(anchor.href)); }
  function detectChallenge(doc) {
    const text = normalizeText(doc.body?.innerText || doc.documentElement?.innerText || '').toLowerCase();
    return selectors.challengeText.split(',').some((needle) => text.includes(needle));
  }
  function detectNoResults(doc) {
    const text = normalizeText(doc.body?.innerText || '').toLowerCase();
    return selectors.noResultsText.split(',').some((needle) => text.includes(needle.toLowerCase()));
  }
  function detectEndOfResults(doc) {
    const text = normalizeText(doc.body?.innerText || '').toLowerCase();
    return text.includes("you've reached the end") || text.includes('fim da lista');
  }
  function extractPhone(root) {
    const button = root?.querySelector?.(selectors.phoneButtons);
    return normalizeText(button?.textContent || button?.getAttribute?.('aria-label') || '') || null;
  }
  function extractWebsite(root) {
    const links = Array.from(root?.querySelectorAll?.(selectors.websiteLinks) || []);
    const link = links.find((item) => normalizeUrl(item.href) && !normalizeMapsUrl(item.href));
    return normalizeUrl(link?.href);
  }
  function extractLabeledText(root, labels) {
    const candidates = Array.from(root?.querySelectorAll?.('[aria-label], button, div, span') || []);
    for (const candidate of candidates) {
      const text = normalizeText(candidate.getAttribute?.('aria-label') || candidate.textContent || '');
      const matched = labels.some((label) => text.toLowerCase().includes(label.toLowerCase()));
      if (matched) return text.replace(new RegExp(`^(${labels.join('|')})\s*:?\s*`, 'i'), '') || null;
    }
    return null;
  }
  function normalizeText(value) { return typeof value === 'string' ? value.trim().replace(/\s+/g, ' ') : ''; }
  function normalizeUrl(value) {
    const text = normalizeText(value);
    if (!text) return null;
    try {
      const url = new URL(text);
      if (!['http:', 'https:'].includes(url.protocol)) return null;
      url.hash = '';
      url.hostname = url.hostname.toLowerCase();
      return url.toString();
    } catch { return null; }
  }
  function normalizeMapsUrl(value) {
    const url = normalizeUrl(value);
    if (!url) return null;
    try {
      const parsed = new URL(url);
      return parsed.hostname.endsWith('google.com') && parsed.pathname.startsWith('/maps') ? parsed.toString() : null;
    } catch { return null; }
  }
  function extractExternalId(mapsUrl = '') {
    try {
      const url = new URL(mapsUrl);
      const cid = url.searchParams.get('cid');
      if (cid) return `cid:${cid}`;
      const match = decodeURIComponent(url.href).match(/!1s([^!]+)/);
      return match?.[1] || null;
    } catch { return null; }
  }
  function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }
})();
