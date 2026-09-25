import { GoogleMapsMessages } from './protocol.js';
import { buildGoogleMapsSearchUrl, isAllowedGoogleMapsUrl } from './url_builder.js';
import { GoogleMapsToolError } from './errors.js';
import { mergeBusiness } from './dedupe.js';

const CONTENT_SCRIPT_FILE = 'src/tools/google_maps/content_script.js';

export class GoogleMapsBrowserController {
  constructor(chromeApi, { navigationTimeoutMs = 30000, readyTimeoutMs = 20000, scrollTimeoutMs = 12000 } = {}) {
    this.chromeApi = chromeApi;
    this.navigationTimeoutMs = navigationTimeoutMs;
    this.readyTimeoutMs = readyTimeoutMs;
    this.scrollTimeoutMs = scrollTimeoutMs;
  }

  async withDedicatedTab(callback) {
    let tab;
    try {
      tab = await this.chromeApi.tabs.create({ url: 'about:blank', active: false });
      if (!tab?.id) throw new GoogleMapsToolError('handler_error', 'tab_create_failed');
      return await callback(new GoogleMapsTabSession(this.chromeApi, tab.id, this));
    } finally {
      if (tab?.id) {
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
    await this.navigate(url);
    await this.ensureContentScript();
    const ready = await this.send({ type: GoogleMapsMessages.WAIT_READY, timeoutMs: this.controller.readyTimeoutMs });
    if (ready?.challenge) throw new GoogleMapsToolError('google_challenge', 'Google challenge detected.');
    if (ready?.noResults) return { businesses: [], found: 0 };
    if (!ready?.ready) throw new GoogleMapsToolError(ready?.code || 'page_structure_changed', 'Google Maps result feed was not found.');
    return this.collectFeed(query, options.maxResults || 100);
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
    await this.chromeApi.scripting.executeScript({ target: { tabId: this.tabId }, files: [CONTENT_SCRIPT_FILE] });
    this.injected = true;
  }

  async collectFeed(sourceQuery, maxResults) {
    const businesses = [];
    let staleRounds = 0;
    let previousCount = 0;
    let emptyRounds = 0;
    while (businesses.length < maxResults && staleRounds < 3 && emptyRounds < 8) {
      const response = await this.send({ type: GoogleMapsMessages.COLLECT_FEED, sourceQuery, limit: maxResults });
      if (response?.challenge) throw new GoogleMapsToolError('google_challenge', 'Google challenge detected.');
      if (!response?.ok) throw new GoogleMapsToolError(response?.code || 'page_structure_changed', 'Google Maps feed collection failed.');
      if (!Array.isArray(response.businesses) || response.businesses.length === 0) {
        if (response.endOfResults) break;
        emptyRounds += 1;
        await this.send({ type: GoogleMapsMessages.SCROLL, timeoutMs: this.controller.scrollTimeoutMs });
        await sleep(800);
        continue;
      }
      emptyRounds = 0;
      const enriched = await this.enrichDetails(response.businesses.slice(0, maxResults));
      businesses.splice(0, businesses.length, ...enriched);
      if (businesses.length >= maxResults || response.endOfResults) break;
      await this.send({ type: GoogleMapsMessages.SCROLL, timeoutMs: this.controller.scrollTimeoutMs });
      if (businesses.length <= previousCount) staleRounds += 1;
      previousCount = businesses.length;
    }
    if (businesses.length === 0) {
      throw new GoogleMapsToolError('results_not_loaded', 'Google Maps did not expose recognizable business cards in time.');
    }
    return { businesses: businesses.slice(0, maxResults), found: businesses.length };
  }

  async enrichDetails(businesses) {
    const enriched = [];
    for (const business of businesses) {
      if (!needsDetail(business)) {
        enriched.push(business);
        continue;
      }
      try {
        const opened = await this.send({ type: GoogleMapsMessages.OPEN_RESULT, mapsUrl: business.maps_url });
        if (!opened?.ok) {
          enriched.push(business);
          continue;
        }
        const detail = await this.send({ type: GoogleMapsMessages.EXTRACT_DETAIL, sourceQuery: business.source_query });
        enriched.push(detail?.business ? mergeBusiness(business, detail.business) : business);
      } catch {
        enriched.push(business);
      }
    }
    return enriched;
  }

  send(message) {
    return this.chromeApi.tabs.sendMessage(this.tabId, message);
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function needsDetail(business) {
  return !business.category || !business.address || !business.phone || !business.website;
}
