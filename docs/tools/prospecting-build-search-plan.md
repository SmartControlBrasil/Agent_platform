# Prospecting Build Search Plan

Slug: `prospecting.build_search_plan`
Runtime handler: `prospecting_build_search_plan`

This deterministic local tool builds a list of prospecting search queries from the Prospecting Agent configuration. It performs no external calls, scraping, AI inference, or secret handling.

## Binding Configuration

- `max_queries`: integer from 1 to 20. Default: 8.

## Input

- `target_market`
- `target_region`
- `target_profile`
- `objective`

## Output

The tool returns status `planned` and an output payload containing the generated `queries` plus the normalized prospecting fields. Access is only allowed through an enabled `AgentToolBinding` for the exact `AgentInstallation`.
