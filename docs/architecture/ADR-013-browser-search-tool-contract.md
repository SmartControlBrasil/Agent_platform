# ADR-013: Browser Search Tool Contract

## Status

Accepted

## Context

Prospecting needs a future browser-executed Google Maps search capability, but the backend must establish the contract before any scraping or browser automation exists. The platform already supports delegated tools, executor credentials, queue claiming, and executor capabilities.

## Decision

Register `prospecting.search_google_maps` as a delegated `ToolDefinition` with no runtime handler. The backend owns contract validation for inputs and completion results, while browser executors remain responsible for the eventual external interaction.

The contract is enforced through extensible validator registries used by `execute_tool` and `complete_tool_execution`. This keeps tool-specific validation out of the core lifecycle while preserving generic behavior for existing tools.

Executions follow the existing delegated lifecycle:

1. A Prospecting installation with an enabled binding calls `execute_tool`.
2. The input contract is validated before any `ToolExecution` is created.
3. The execution is created and dispatched.
4. A browser executor may claim it only with an enabled `ToolExecutorCapability`.
5. Completion validates the result before `SUCCEEDED`.
6. Invalid results transition to `FAILED` with `invalid_tool_result` and no official invalid result payload.

## Consequences

- No Google Maps scraping, selectors, Chrome control, enrichment, coordinates, or API calls are introduced by this phase.
- The tool is visible and bindable for Prospecting, but it is not auto-triggered by the agent.
- Executor capability grants remain explicit and auditable.
- The Control Plane can present a safe summary for Maps executions without exposing raw payloads indiscriminately.
- Future delegated tools can reuse the validator registry pattern instead of adding scattered lifecycle branches.
