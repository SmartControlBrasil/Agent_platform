# Prospecting Agent

## Purpose

Prospecting Agent is the second concrete runtime of the Agent Platform.

Its initial role is architectural: prove that a non-Lívia agent can be registered, installed, configured, and executed through the generic runtime registry and ports without changing the generic execution endpoint.

## Configuration

Current supported fields:

- `target_market`
- `target_region`
- `target_profile`
- `objective`
- `max_results`

Validation rules:

- strings are stripped and length-limited;
- `max_results` must be an integer between `1` and `500`;
- plaintext secrets are not allowed in `AgentInstallation.configuration`.

## Current Version

- AgentDefinition slug: `prospecting`
- AgentVersion: `1.0.0`
- Runtime handler: `prospecting`

## Operational Context

The operational prospecting workflow lives in the same repository under the `prospecting` app and is exposed in the Hando portal (`/painel/prospeccao/...`), not in a parallel UI. This workflow includes:

- `SearchRun` and `SearchResult` for discovery records;
- explicit promotion `SearchResult -> Prospect`;
- `ProspectSource` provenance history;
- `ProspectEnrichment` manual observations;
- optional website enrichment with strict SSRF controls.

## Current Limitations

Still out of scope in the current platform phase:

- Google Maps scraping or browser automation from this app;
- smart_sales runtime coupling;
- AI/LLM scoring or autonomous qualification;
- CRM pipeline management and outreach orchestration.

## Future Planned Tools

Possible future capabilities include:

- prospect source connectors,
- enrichment tools,
- qualification pipelines,
- async execution orchestration,
- customer-product integrations such as `smart_sales`.
