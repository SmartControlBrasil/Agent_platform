# prospecting.search_google_maps

`prospecting.search_google_maps` is a delegated prospecting tool contract for browser executors. It defines the backend registration, input and output contracts, lifecycle validation, capability gating, and Control Plane presentation. It does not implement Google Maps scraping, selectors, browser automation, enrichment, coordinates, or any Google API integration.

## Tool Definition

- Slug: `prospecting.search_google_maps`
- Name: `Search Google Maps`
- Category: `prospecting`
- Execution mode: `DELEGATED`
- Runtime handler: empty
- Compatible agent: `prospecting`
- Executor requirement: Browser executor with an explicit `ToolExecutorCapability`

No `AgentToolBinding` or executor capability is granted automatically by the bootstrap migration.

## Input Contract V1

```json
{
  "schema_version": 1,
  "queries": ["hospital privado São Paulo"],
  "target_region": "São Paulo",
  "max_results": 50,
  "locale": "pt-BR"
}
```

Rules:

- `schema_version` is required and must be `1`.
- `queries` is required, must contain 1 to 20 non-empty strings, and each query is normalized for whitespace.
- Query strings must be search terms. Arbitrary URLs are rejected as query substitutes.
- `target_region` is optional and normalized when present.
- `max_results` is required and must be an integer from 1 to 100.
- `locale` defaults to `pt-BR` when omitted.
- Plaintext secret-like keys are rejected, including password, token, secret, api_key, authorization, cookie, and credential. Redacted markers such as `[redacted]` are allowed.

## Output Contract V1

```json
{
  "schema_version": 1,
  "status": "completed",
  "businesses": [
    {
      "name": "Hospital A",
      "category": "Hospital",
      "address": "Rua A, 1",
      "phone": null,
      "website": "https://example.com/",
      "maps_url": "https://maps.google.com/?cid=1",
      "external_id": "cid:1",
      "source_query": "hospital privado São Paulo"
    }
  ],
  "stats": {
    "queries_requested": 1,
    "queries_executed": 1,
    "businesses_found": 1,
    "businesses_returned": 1,
    "duplicates_removed": 0,
    "duration_ms": 1200
  }
}
```

Rules:

- `schema_version` is required and must be `1`.
- `status` is required.
- `businesses` is required and must not exceed the request `max_results` or the global limit of 100.
- Each business requires `name` and either `maps_url` or `external_id`.
- `category`, `address`, `phone`, `website`, and `source_query` may be null when Google Maps does not expose them.
- URL fields must be absolute HTTP or HTTPS URLs.
- The contract does not require phone or website and must not invent them.
- Results are deduplicated within one payload by `external_id`, then normalized `maps_url`, then normalized `name + address`.
- Stats are accepted only as non-negative integers. Critical rules are enforced from the validated payload, not trusted stats.
- Invalid completion payloads mark the execution as `FAILED` with `invalid_tool_result`; the invalid payload is not stored as the official result.

## Executor Failure Codes

Executors should use these failure codes when reporting controlled failures:

- `unsupported_tool`
- `google_maps_unavailable`
- `google_challenge`
- `human_action_required`
- `navigation_timeout`
- `page_structure_changed`
- `execution_cancelled`
- `handler_error`

## Control Plane

The tool appears in the catalog and can be added to Prospecting installations through the generic Add Tool flow. Executor access is managed separately through generic executor capabilities. The Tool Execution detail page shows requested queries, result count, status, executor, duration, and a preview of the first 10 businesses instead of raw payload dumps.
