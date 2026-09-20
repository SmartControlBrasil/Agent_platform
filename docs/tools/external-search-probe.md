# External Search Probe

Slug: `prospecting.external_search_probe`
Execution mode: `DELEGATED`
Runtime handler: none

This laboratory tool proves the delegated execution lifecycle. It does not execute search, scrape sites, call Google Maps, call AI, or contact external services.

## Lifecycle

1. A compatible Prospecting installation may bind the tool explicitly.
2. `execute_tool(...)` creates a `ToolExecution`.
3. The no-op dispatcher marks it `DISPATCHED`.
4. A test or future external executor with `ToolExecutorCapability` claims it.
5. The executor completes or fails it.

## Security Notes

The tool is not auto-bound to installations. Executors need explicit capability and tenant-compatible ownership. Payloads must not include plaintext secrets.
