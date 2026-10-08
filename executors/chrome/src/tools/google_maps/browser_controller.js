import { GoogleMapsMessages } from './protocol.js';
import { buildGoogleMapsSearchUrl, isAllowedGoogleMapsUrl } from './url_builder.js';
import { GoogleMapsToolError } from './errors.js';
import { mergeBusiness } from './dedupe.js';
import { createMapsDiagnostics, summarizeMapsUrl } from './diagnostics.js';

const DOM_EXTRACTION_FILE = 'src/tools/google_maps/dom_extraction.js';
const CONTENT_SCRIPT_FILE = 'src/tools/google_maps/content_script.js';

export class GoogleMapsBrowserController {
  constructor(chromeApi, { navigationTimeoutMs = 30000, readyTimeoutMs = 30000, scrollTimeoutMs = 12000, logger = null, keepTabOpenOnError = false } = {}) {
    this.chromeApi = chromeApi;
    this.navigationTimeoutMs = navigationTimeoutMs;
    this.readyTimeoutMs = readyTimeoutMs;
    this.scrollTimeoutMs = scrollTimeoutMs;
    this.logger = logger;
    this.keepTabOpenOnError = keepTabOpenOnError;
    this.diagnostics = createMapsDiagnostics(logger);
  }

  async withDedicatedTab(callback) {
    let tab;
    let failed = false;
    try {
      tab = await this.chromeApi.tabs.create({ url: 'about:blank', active: false });
      if (!tab?.id) throw new GoogleMapsToolError('handler_error', 'tab_create_failed');
      return await callback(new GoogleMapsTabSession(this.chromeApi, tab.id, this));
    } catch (error) {
      failed = true;
      throw error;
    } finally {
      if (tab?.id && !(failed && this.keepTabOpenOnError)) {
        try { await this.chromeApi.tabs.remove(tab.id); } catch {}
      }
    }
  }
}

export class GoogleMapsTabSession {
  constructor(chromeApi, tabId, controller) {
    this.chromeApi = chromeApi;
    this.tabId = tabId;
    this.controller = controller;
    this.injected = false;
  }

  async search(query, options = {}) {
    const url = buildGoogleMapsSearchUrl(query, options);
    if (!isAllowedGoogleMapsUrl(url)) throw new GoogleMapsToolError('handler_error', 'invalid_maps_url');
    this.controller.diagnostics.stage('search_started', {
      queryLength: String(query || '').length,
      strategy: 'url_search',
      requestedUrl: summarizeMapsUrl(url),
    });
    await this.navigate(url);
    await this.assertMapsTabOpened();
    await this.ensureContentScript();
    const ready = await this.send({ type: GoogleMapsMessages.WAIT_READY, timeoutMs: this.controller.readyTimeoutMs });
    if (ready?.challenge) throw new GoogleMapsToolError('captcha_or_blocked', 'Google challenge detected.', diagnosticsFor('captcha_or_blocked', query, ready?.diagnostics));
    if (ready?.noResults) {
      this.controller.diagnostics.stage('search_no_results', { signal: ready?.signal || 'no_results_text' });
      return { businesses: [], found: 0 };
    }
    if (!ready?.ready) {
      this.controller.diagnostics.stage('search_wait_failed', {
        code: ready?.code || 'results_feed_not_found',
        signal: ready?.signal,
        diagnostics: ready?.diagnostics,
      });
      throw new GoogleMapsToolError(ready?.code || 'results_feed_not_found', 'Google Maps result feed was not found.', diagnosticsFor('search_wait_failed', query, ready?.diagnostics, { signal: ready?.signal, code: ready?.code }));
    }
    this.controller.diagnostics.stage('search_results_ready', { signal: ready?.signal || 'unknown' });
    return this.collectFeed(query, options.maxResults || 100, options);
  }

  async assertMapsTabOpened() {
    const tab = await this.chromeApi.tabs.get(this.tabId);
    const url = tab?.url || '';
    this.controller.diagnostics.stage('tab_loaded', { url, tabStatus: tab?.status || '' });
    if (!url || url === 'about:blank') {
      throw new GoogleMapsToolError('maps_page_not_opened', 'Google Maps tab did not open.');
    }
    if (url.includes('consent.google.com')) {
      throw new GoogleMapsToolError('consent_screen', 'Google consent screen detected.');
    }
    if (!isAllowedGoogleMapsUrl(url)) {
      throw new GoogleMapsToolError('maps_page_not_opened', 'Tab URL is not an allowed Google Maps page.');
    }
  }

  async navigate(url) {
    await this.chromeApi.tabs.update(this.tabId, { url });
    await this.waitForTabComplete();
    this.injected = false;
  }

  async waitForTabComplete() {
    const deadline = Date.now() + this.controller.navigationTimeoutMs;
    while (Date.now() < deadline) {
      const tab = await this.chromeApi.tabs.get(this.tabId);
      if (tab?.status === 'complete') return;
      await sleep(250);
    }
    throw new GoogleMapsToolError('navigation_timeout', 'Google Maps navigation timed out.');
  }

  async ensureContentScript() {
    if (this.injected) return;
    await this.chromeApi.scripting.executeScript({ target: { tabId: this.tabId }, files: [DOM_EXTRACTION_FILE, CONTENT_SCRIPT_FILE] });
    this.injected = true;
  }

  async collectFeed(sourceQuery, maxResults, searchOptions = {}) {
    const businesses = [];
    let staleRounds = 0;
    let previousCount = 0;
    let emptyRounds = 0;
    while (businesses.length < maxResults && staleRounds < 3 && emptyRounds < 8) {
      const response = await this.send({ type: GoogleMapsMessages.COLLECT_FEED, sourceQuery, limit: maxResults });
      if (response?.challenge) throw new GoogleMapsToolError('captcha_or_blocked', 'Google challenge detected.', diagnosticsFor('captcha_or_blocked', sourceQuery, response?.diagnostics));
      if (!response?.ok) {
        this.controller.diagnostics.stage('collect_feed_failed', {
          code: response?.code || 'results_feed_not_found',
          diagnostics: response?.diagnostics,
        });
        throw new GoogleMapsToolError(response?.code || 'results_feed_not_found', 'Google Maps feed collection failed.', diagnosticsFor('collect_feed_failed', sourceQuery, response?.diagnostics, { code: response?.code }));
      }
      if (!Array.isArray(response.businesses) || response.businesses.length === 0) {
        if (response.diagnostics) {
          this.controller.diagnostics.stage('collect_feed_empty_round', { diagnostics: response.diagnostics, emptyRounds });
        }
        if (response.endOfResults) break;
        emptyRounds += 1;
        await this.send({ type: GoogleMapsMessages.SCROLL, timeoutMs: this.controller.scrollTimeoutMs });
        await sleep(800);
        continue;
      }
      emptyRounds = 0;
      const enriched = await this.enrichDetails(response.businesses.slice(0, maxResults), sourceQuery, searchOptions);
      businesses.splice(0, businesses.length, ...enriched);
      if (businesses.length >= maxResults || response.endOfResults) break;
      await this.send({ type: GoogleMapsMessages.SCROLL, timeoutMs: this.controller.scrollTimeoutMs });
      if (businesses.length <= previousCount) staleRounds += 1;
      previousCount = businesses.length;
    }
    if (businesses.length === 0) {
      this.controller.diagnostics.stage('collect_feed_empty', { sourceQueryLength: String(sourceQuery || '').length, maxResults, emptyRounds });
      throw new GoogleMapsToolError('results_timeout', 'Google Maps did not expose recognizable business cards in time.', diagnosticsFor('collect_feed_empty', sourceQuery, null, { processed_results: businesses.length }));
    }
    return { businesses: businesses.slice(0, maxResults), found: businesses.length };
  }

  async enrichDetails(businesses, sourceQuery, searchOptions = {}) {
    const enriched = [];
    for (const business of businesses) {
      if (!needsDetail(business)) {
        enriched.push(business);
        continue;
      }
      const mapsUrl = business.maps_url;
      if (!mapsUrl || !isAllowedGoogleMapsUrl(mapsUrl)) {
        enriched.push(business);
        continue;
      }
      try {
        await this.navigate(mapsUrl);
        await this.ensureContentScript();
        const ready = await this.send({
          type: GoogleMapsMessages.WAIT_DETAIL,
          timeoutMs: this.controller.readyTimeoutMs,
        });
        if (ready?.challenge) {
          enriched.push(business);
          continue;
        }
        const detail = await this.send({
          type: GoogleMapsMessages.EXTRACT_DETAIL,
          sourceQuery: business.source_query || sourceQuery,
        });
        enriched.push(detail?.business ? mergeBusiness(business, detail.business) : business);
      } catch {
        enriched.push(business);
      }
    }
    const backUrl = buildGoogleMapsSearchUrl(sourceQuery, { locale: searchOptions.locale || 'pt-BR' });
    if (isAllowedGoogleMapsUrl(backUrl)) {
      try {
        await this.navigate(backUrl);
        await this.ensureContentScript();
      } catch {
        /* feed restore is best-effort */
      }
    }
    return enriched;
  }

  send(message) {
    return this.chromeApi.tabs.sendMessage(this.tabId, message);
  }
}

function diagnosticsFor(stage, query, diagnostics = null, extra = {}) {
  return {
    stage,
    current_query: query,
    ...(diagnostics || {}),
    ...extra,
  };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function needsDetail(business) {
  return !business.category || !business.address || !business.phone || !business.website;
}
