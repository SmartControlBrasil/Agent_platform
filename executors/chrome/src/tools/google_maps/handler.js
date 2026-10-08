import { validateGoogleMapsInput } from './input_contract.js';
import { dedupeBusinesses } from './dedupe.js';
import { normalizeBusiness } from './normalization.js';
import { GoogleMapsBrowserController } from './browser_controller.js';

export class GoogleMapsSearchHandler {
  constructor({ controller = null, chromeApi = null, now = () => Date.now(), logger = console, progressReporter = null } = {}) {
    this.controller = controller;
    this.chromeApi = chromeApi;
    this.now = now;
    this.logger = logger;
    this.progressReporter = progressReporter;
  }

  async handle(requestPayload, execution = {}) {
    const startedAt = this.now();
    const input = validateGoogleMapsInput(requestPayload);
    const controller =
      this.controller ||
      new GoogleMapsBrowserController(this.chromeApi || globalThis.chrome, { logger: this.logger });
    const allBusinesses = [];
    let businessesFound = 0;
    let queriesExecuted = 0;
    let duplicatesRemoved = 0;

    await controller.withDedicatedTab(async (tab) => {
      for (const [index, query] of input.queries.entries()) {
        if (allBusinesses.length >= input.max_results) break;
        this.safeLog('info', 'google_maps.query_started', { executionId: execution.id, queryIndex: index });
        await this.reportProgress({
          stage: 'opening_google_maps',
          current_query: query,
          detected_results: businessesFound,
          processed_results: allBusinesses.length,
        });
        const remaining = input.max_results - allBusinesses.length;
        await this.reportProgress({
          stage: 'detecting_results',
          current_query: query,
          detected_results: businessesFound,
          processed_results: allBusinesses.length,
        });
        const result = await tab.search(query, { locale: input.locale, maxResults: remaining });
        queriesExecuted += 1;
        businessesFound += result.found || result.businesses?.length || 0;
        await this.reportProgress({
          stage: 'collecting_results',
          current_query: query,
          detected_results: businessesFound,
          processed_results: allBusinesses.length,
        });
        for (const raw of result.businesses || []) {
          const normalized = normalizeBusiness(raw, query);
          if (normalized) allBusinesses.push(normalized);
        }
        const deduped = dedupeBusinesses(allBusinesses, input.max_results);
        duplicatesRemoved += deduped.duplicatesRemoved;
        allBusinesses.splice(0, allBusinesses.length, ...deduped.businesses);
        await this.reportProgress({
          stage: 'query_completed',
          current_query: query,
          detected_results: businessesFound,
          processed_results: allBusinesses.length,
        });
        this.safeLog('info', 'google_maps.query_completed', { executionId: execution.id, queryIndex: index, resultCount: allBusinesses.length });
      }
    });

    const deduped = dedupeBusinesses(allBusinesses, input.max_results);
    duplicatesRemoved += deduped.duplicatesRemoved;
    const durationMs = Math.max(0, this.now() - startedAt);
    return {
      schema_version: 1,
      status: 'completed',
      businesses: deduped.businesses,
      stats: {
        queries_requested: input.queries.length,
        queries_executed: queriesExecuted,
        businesses_found: businessesFound,
        businesses_returned: deduped.businesses.length,
        duplicates_removed: duplicatesRemoved,
        duration_ms: durationMs,
      },
    };
  }

  async reportProgress(progress) {
    if (!this.progressReporter) return;
    await this.progressReporter(progress);
  }

  safeLog(level, message, metadata = {}) {
    const log = this.logger?.[level] || this.logger?.log;
    if (!log) return;
    log.call(this.logger, message, metadata);
  }
}
