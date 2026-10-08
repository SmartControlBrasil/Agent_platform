(() => {
  const Messages = Object.freeze({
    WAIT_READY: 'GMAPS_WAIT_READY',
    WAIT_DETAIL: 'GMAPS_WAIT_DETAIL',
    COLLECT_FEED: 'GMAPS_COLLECT_FEED',
    OPEN_RESULT: 'GMAPS_OPEN_RESULT',
    EXTRACT_DETAIL: 'GMAPS_EXTRACT_DETAIL',
    SCROLL: 'GMAPS_SCROLL',
    DETECT_CHALLENGE: 'GMAPS_DETECT_CHALLENGE',
  });
  const allowedMessages = new Set(Object.values(Messages));
  const selectors = Object.freeze({
    resultsFeed: '[role="feed"]',
    resultAnchors:
      'a[href*="/maps/place/"], a[href*="/maps/search/"], a[href*="/maps?cid="], a[href*="/maps?ll="]',
    placePanel: '[role="main"]',
    websiteLinks: 'a[data-item-id="authority"], a[aria-label^="Website"], a[href^="http"]',
    phoneButtons:
      'button[data-item-id^="phone"], button[aria-label*="Phone"], button[aria-label*="Telefone"]',
    noResultsText: 'No results found,Nenhum resultado encontrado',
    challengeText:
      'captcha,unusual traffic,not a robot,verifique se você não é um robô,tráfego incomum',
    consentText:
      'before you continue,antes de continuar,accept all,aceitar tudo,rejeitar tudo',
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
    if (message.type === Messages.WAIT_DETAIL) return waitDetail(message.timeoutMs || 20000);
    if (message.type === Messages.COLLECT_FEED) return collectFeed(document, message.sourceQuery || '', message.limit || 100);
    if (message.type === Messages.SCROLL) return scrollFeed(document, message.timeoutMs || 12000);
    if (message.type === Messages.EXTRACT_DETAIL) return { ok: true, business: extractDetailBusiness(document, message.sourceQuery || '') };
    if (message.type === Messages.OPEN_RESULT) return openResult(document, message.mapsUrl || '');
    return { ok: false, code: 'unsupported_message' };
  }

  async function waitReady(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (detectChallenge(document)) return { ok: false, ready: false, code: 'captcha_or_blocked' };
      if (detectConsent(document)) return { ok: false, ready: false, code: 'consent_screen' };
      const loaded = detectResultsLoaded(document);
      if (loaded.noResults) return { ok: true, ready: true, noResults: true, signal: loaded.signal, diagnostics: loaded.diagnostics };
      if (loaded.ready) return { ok: true, ready: true, signal: loaded.signal, diagnostics: loaded.diagnostics };
      await sleep(250);
    }
    const snapshot = snapshotLoadSignals(document);
    return { ok: false, ready: false, code: 'results_timeout', diagnostics: snapshot };
  }

  function collectFeed(doc, sourceQuery, limit) {
    if (detectChallenge(doc)) return { ok: false, code: 'captcha_or_blocked', businesses: [] };
    if (detectConsent(doc)) return { ok: false, code: 'consent_screen', businesses: [] };
    const loaded = detectResultsLoaded(doc);
    if (!loaded.ready && !detectNoResults(doc)) {
      return { ok: false, code: 'results_feed_not_found', businesses: [], diagnostics: snapshotLoadSignals(doc) };
    }
    const anchors = findResultAnchors(doc);
    const businesses = anchors.slice(0, limit).map((anchor) => businessFromAnchor(anchor, sourceQuery)).filter(Boolean);
    if (businesses.length === 0 && snapshotLoadSignals(doc).articleCount > 0) {
      return { ok: false, code: 'result_cards_without_place_links', businesses: [], diagnostics: snapshotLoadSignals(doc) };
    }
    return { ok: true, businesses, diagnostics: snapshotLoadSignals(doc), endOfResults: detectEndOfResults(doc) || businesses.length >= limit };
  }

  async function scrollFeed(doc, timeoutMs) {
    const feed = findResultsFeed(doc) || doc.scrollingElement || doc.documentElement;
    const before = feed.scrollTop || 0;
    feed.scrollTop = before + Math.max(feed.clientHeight || 800, 600);
    feed.dispatchEvent?.(new Event('scroll', { bubbles: true }));
    await sleep(Math.min(timeoutMs, 800));
    return { ok: true };
  }

  function domExtract() {
    return globalThis.__agentExecutorGoogleMapsDomExtraction || {};
  }

  function businessFromAnchor(anchor, sourceQuery) {
    const mapsUrl = normalizeMapsUrl(anchor.href);
    const container = anchor.closest('[role="article"], [role="feed"] > div, div') || anchor.parentElement;
    const heading = container?.querySelector?.('[role="heading"], h1, h2, h3');
    const name = normalizeText(anchor.getAttribute('aria-label') || anchor.textContent || heading?.textContent || '');
    if (!mapsUrl || !name) return null;
    const extract = domExtract();
    return {
      name,
      category: (extract.extractLabeledText || extractLabeledText)(container, ['Category', 'Categoria']),
      address: extract.extractAddress ? extract.extractAddress(container) : extractLabeledText(container, ['Address', 'Endereço']),
      phone: (extract.extractPhone || extractPhone)(container),
      website: (extract.extractWebsite || extractWebsite)(container),
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

  function findDetailRoot(doc) {
    const phone = doc.querySelector('button[data-item-id^="phone"]');
    const address = doc.querySelector('button[data-item-id="address"], button[data-item-id^="address:"]');
    const marker = phone || address;
    if (marker) {
      const main = marker.closest('[role="main"]');
      return main || marker.closest('div') || doc.body;
    }
    return doc.querySelector(selectors.placePanel) || doc.body;
  }

  async function waitDetail(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (detectChallenge(document)) return { ok: true, ready: false, challenge: true };
      const panel = findDetailRoot(document);
      const extract = domExtract();
      if ((extract.extractPhone && extract.extractPhone(panel)) || (extract.extractAddress && extract.extractAddress(panel))) {
        return { ok: true, ready: true };
      }
      await sleep(300);
    }
    return { ok: true, ready: false, code: 'detail_timeout' };
  }

  function extractDetailBusiness(doc, sourceQuery) {
    const panel = findDetailRoot(doc);
    const heading = panel.querySelector('h1, [role="heading"]');
    const mapsUrl = normalizeMapsUrl(location.href);
    const extract = domExtract();
    return {
      name: normalizeText(heading?.textContent || ''),
      category: (extract.extractLabeledText || extractLabeledText)(panel, ['Category', 'Categoria']),
      address: extract.extractAddress ? extract.extractAddress(panel) : extractLabeledText(panel, ['Address', 'Endereço']),
      phone: (extract.extractPhone || extractPhone)(panel),
      website: (extract.extractWebsite || extractWebsite)(panel),
      maps_url: mapsUrl,
      external_id: extractExternalId(mapsUrl),
      source_query: sourceQuery,
    };
  }

  function findResultsFeed(doc) {
    return doc.querySelector(selectors.resultsFeed);
  }

  function findResultAnchors(doc) {
    return Array.from(doc.querySelectorAll(selectors.resultAnchors)).filter((anchor) => normalizeMapsUrl(anchor.href));
  }

  function findResultArticles(doc) {
    return Array.from(doc.querySelectorAll('[role="article"]'));
  }

  function findSearchInput(doc) {
    return doc.querySelector('input[aria-label*="Search"], input[aria-label*="Pesquisar"], input[role="combobox"], input[name="q"]');
  }

  function detectResultsLoaded(doc) {
    const diagnostics = snapshotLoadSignals(doc);
    if (detectNoResults(doc)) return { ready: true, noResults: true, signal: 'no_results_text', diagnostics };
    const anchors = findResultAnchors(doc);
    if (anchors.length > 0) return { ready: true, signal: 'result_anchors', anchorCount: anchors.length, diagnostics };
    const feed = findResultsFeed(doc);
    if (feed) {
      const feedAnchors = feed.querySelectorAll('a[href*="/maps/place/"]');
      if (feedAnchors.length > 0) {
        return { ready: true, signal: 'feed_place_anchors', anchorCount: feedAnchors.length, diagnostics };
      }
      if (feed.querySelector('[role="article"]')) {
        return { ready: true, signal: 'feed_articles', diagnostics };
      }
    }
    if (findResultArticles(doc).length > 0) return { ready: true, signal: 'articles_without_feed', diagnostics };
    return { ready: false, signal: 'awaiting_results', diagnostics };
  }

  function snapshotLoadSignals(doc) {
    const feed = findResultsFeed(doc);
    const input = findSearchInput(doc);
    const loc = safeLocationSummary();
    return {
      readyState: String(doc.readyState || '').slice(0, 32),
      pathname: loc.pathname,
      search: loc.search,
      title: normalizeText(doc.title || '').slice(0, 120),
      hasResultsFeed: Boolean(feed),
      feedAriaLabel: normalizeText(feed?.getAttribute?.('aria-label') || '').slice(0, 120),
      placeAnchorCount: findResultAnchors(doc).length,
      articleCount: findResultArticles(doc).length,
      hasArticle: findResultArticles(doc).length > 0,
      hasMapMain: Boolean(doc.querySelector('[role="main"]')),
      hasSearchInput: Boolean(input),
      searchInputAriaLabel: normalizeText(input?.getAttribute?.('aria-label') || '').slice(0, 80),
      hasNoResultsText: detectNoResults(doc),
      hasConsentText: detectConsent(doc),
      hasChallengeText: detectChallenge(doc),
    };
  }

  function safeLocationSummary() {
    try {
      const url = new URL(location.href);
      const query = url.searchParams.get('query') || url.searchParams.get('q') || '';
      const params = [];
      if (url.searchParams.has('api')) params.push('api');
      if (url.searchParams.has('query')) params.push(`query:${query.length}`);
      if (url.searchParams.has('q')) params.push(`q:${query.length}`);
      if (url.searchParams.has('hl')) params.push(`hl:${String(url.searchParams.get('hl') || '').slice(0, 16)}`);
      return { pathname: url.pathname.slice(0, 120), search: params.join('&') };
    } catch {
      return { pathname: '', search: 'invalid_url' };
    }
  }
  function detectChallenge(doc) {
    const text = normalizeText(doc.body?.innerText || doc.documentElement?.innerText || '').toLowerCase();
    return selectors.challengeText.split(',').some((needle) => text.includes(needle));
  }
  function detectConsent(doc) {
    const text = normalizeText(doc.body?.innerText || doc.documentElement?.innerText || '').toLowerCase();
    return selectors.consentText.split(',').some((needle) => text.includes(needle.toLowerCase()));
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
