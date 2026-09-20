# Prospecting Agent

## Purpose

Prospecting Agent is the second concrete runtime of the Agent Platform.

Its current purpose is to validate that a non-Lívia agent can be:

- registered in the catalog,
- installed through the Control Plane,
- configured with its own schema,
- executed through the generic installation execution API.

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

## Current Limitations

This phase does not implement:

- external APIs,
- scraping,
- Google Maps or Google Search,
- smart_sales integration,
- AI or LLM generation,
- lead import,
- execution jobs or async orchestration.

The runtime only returns a structured readiness response proving correct runtime resolution and configuration isolation.

## Future Planned Tools

Possible future capabilities include:

- prospect source connectors,
- enrichment tools,
- qualification pipelines,
- async execution orchestration,
- customer-product integrations such as `smart_sales`.
