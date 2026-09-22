import { fail } from './errors.js';

export const GOOGLE_MAPS_SCHEMA_VERSION = 1;
export const GOOGLE_MAPS_MAX_QUERIES = 20;
export const GOOGLE_MAPS_MAX_RESULTS = 100;
export const DEFAULT_LOCALE = 'pt-BR';

export function validateGoogleMapsInput(requestPayload = {}) {
  const input = requestPayload?.input || requestPayload || {};
  if (!input || typeof input !== 'object' || Array.isArray(input)) fail('handler_error', 'input must be an object');
  if (input.schema_version !== GOOGLE_MAPS_SCHEMA_VERSION) fail('handler_error', 'schema_version must be 1');
  if (!Array.isArray(input.queries) || input.queries.length < 1) fail('handler_error', 'queries must contain at least one item');
  if (input.queries.length > GOOGLE_MAPS_MAX_QUERIES) fail('handler_error', 'queries cannot contain more than 20 items');
  const queries = input.queries.map(normalizeQuery);
  const maxResults = normalizeInteger(input.max_results, 1, GOOGLE_MAPS_MAX_RESULTS, 'max_results');
  return {
    schema_version: GOOGLE_MAPS_SCHEMA_VERSION,
    queries,
    target_region: normalizeOptionalText(input.target_region, 120),
    max_results: maxResults,
    locale: normalizeOptionalText(input.locale, 16) || DEFAULT_LOCALE,
  };
}

function normalizeQuery(value) {
  const query = normalizeRequiredText(value, 200, 'query');
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(query)) fail('handler_error', 'queries must not be URLs');
  return query;
}

function normalizeRequiredText(value, limit, field) {
  if (typeof value !== 'string') fail('handler_error', `${field} must be a string`);
  const text = value.trim().replace(/\s+/g, ' ');
  if (!text) fail('handler_error', `${field} is required`);
  if (text.length > limit) fail('handler_error', `${field} is too long`);
  return text;
}

function normalizeOptionalText(value, limit) {
  if (value === undefined || value === null || value === '') return '';
  return normalizeRequiredText(value, limit, 'text');
}

function normalizeInteger(value, min, max, field) {
  if (typeof value === 'boolean') fail('handler_error', `${field} must be an integer`);
  const text = String(value ?? '').trim();
  if (!/^\d+$/.test(text)) fail('handler_error', `${field} must be an integer`);
  const number = Number.parseInt(text, 10);
  if (number < min || number > max) fail('handler_error', `${field} must be between ${min} and ${max}`);
  return number;
}
