# Google Maps Prospecting Search

Tool slug: `prospecting.search_google_maps`

This extension executes the delegated browser workflow for the Agent Platform Google Maps prospecting contract. It uses the normal executor queue: poll, claim, run in Chrome, complete, or fail with a controlled error. It does not create leads, prospects, CRM records, Smart Sales data, emails, WhatsApp messages, OpenAI calls, proxies, stealth behavior, or CAPTCHA bypasses. In the accepted Smart Sales E2E, it is only the browser executor between Agent Platform and Google Maps.

## Architecture

Service Worker -> ToolDispatcher -> GoogleMapsSearchHandler -> GoogleMapsBrowserController -> Content Script DOM Adapter -> Google Maps page

Responsibilities are split as follows:

- `tool_dispatcher.js`: static registry for supported tools. Unknown slugs fail with `unsupported_tool`.
- `handler.js`: validates input, orchestrates sequential queries, normalizes results, deduplicates, and builds contract output.
- `browser_controller.js`: owns dedicated tab creation, navigation, script injection, message sending, scrolling, and teardown.
- `content_script.js`: reads public DOM data only and answers whitelisted internal messages.
- `input_contract.js`, `normalization.js`, `dedupe.js`, `url_builder.js`: pure testable helpers.

## Permissions

Manifest permissions added for this phase:

- `tabs`: create, navigate, inspect status, and close a dedicated execution tab.
- `scripting`: inject the Google Maps content script into the dedicated tab.
- `host_permissions` for `https://www.google.com/maps*` and `https://www.google.com/maps/*`: limit DOM access to Google Maps pages.

Restricted optional host permissions are used by the options flow for local development and SmartControl Agent Platform origins only; global host patterns are not requested.

## Input

The executor validates the backend contract again:

```json
{
  "schema_version": 1,
  "queries": ["hospital privado Barueri"],
  "target_region": "Barueri",
  "max_results": 10,
  "locale": "pt-BR"
}
```

Limits: 1 to 20 queries and global `max_results` from 1 to 100. Query values must be search terms, not arbitrary URLs.

## Output

The handler returns the backend v1 contract exactly:

```json
{
  "schema_version": 1,
  "status": "completed",
  "businesses": [],
  "stats": {
    "queries_requested": 1,
    "queries_executed": 1,
    "businesses_found": 0,
    "businesses_returned": 0,
    "duplicates_removed": 0,
    "duration_ms": 0
  }
}
```

No raw HTML, cookies, tokens, storage data, or large metadata dumps are sent. Real Maps URLs may exceed the Django default URL length; consumers should allow long `website` and `maps_url` values such as the accepted 271-character Maps URL regression case.

## DOM Strategy

Selectors are centralized in `selectors.js`. The content script prefers semantic signals: roles, accessible labels, Maps place links, website anchors, phone/address buttons, and challenge/no-results text. It avoids relying only on obfuscated CSS classes.

Readiness waits for a result feed, result anchors, no-results state, or challenge state until a bounded timeout. Feed scrolling stops on max results, end-of-results text, repeated lack of growth, timeout, or challenge.

## Detail Extraction

The first version prioritizes stable feed extraction. Detail extraction support exists in the message protocol, but the handler does not force unstable detail navigation when feed data is enough. Missing phone or website is returned as `null` and is not treated as an error.

## Normalization And Dedupe

Strings are trimmed and whitespace-collapsed. Phone values are preserved as displayed. Website and Maps URLs must be valid HTTP(S) URLs. External IDs are extracted only from stable Maps URL identifiers such as `cid`; no hashes are invented.

Deduplication order:

1. `external_id`
2. normalized `maps_url`
3. normalized `name + address`

Duplicate records are merged conservatively, preserving the first value and filling missing fields from later duplicates.

## Error Handling

Controlled failure codes include:

- `unsupported_tool`
- `google_maps_unavailable`
- `google_challenge`
- `human_action_required`
- `navigation_timeout`
- `page_structure_changed`
- `execution_cancelled`
- `handler_error`

CAPTCHA/challenge detection never attempts solving or bypassing. If detected, execution fails with `google_challenge`.

## Privacy

The content script has no executor credential, tenant, platform URL, or backend client. It does not call `fetch`. It only returns selected public business fields rendered on the Google Maps page.

## Manual Development Mode

For development, the service worker accepts an internal extension message:

```js
chrome.runtime.sendMessage({ type: 'AGENT_EXECUTOR_DEV_POLL_NOW' })
```

This triggers the normal queue poll. It does not accept hardcoded execution IDs and does not bypass backend claim/capability checks.

## Limitations

- Queries run sequentially.
- No stealth, proxy rotation, fingerprint spoofing, login automation, or CAPTCHA bypass.
- Google Maps DOM changes can require selector updates.
- Cancellation is internal only for timeouts/teardown; the current backend queue contract does not provide an executor cancellation polling endpoint.
