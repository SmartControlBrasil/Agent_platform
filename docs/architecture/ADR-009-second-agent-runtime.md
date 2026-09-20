# ADR-009: Second Agent Runtime via Registry

## Status

Accepted.

## Context

Phase 4 introduces the Prospecting Agent as the second real runtime in the Agent Platform. The goal is to prove that the architecture supports multiple concrete agents without making the generic core aware of individual implementations.

This phase must not couple the new runtime to `smart_sales`, external APIs, scraping, Google services, or LLM providers.

## Decision

The new runtime is added through the existing runtime registry:

- `livia` -> `LiviaAgentAdapter`
- `prospecting` -> `ProspectingAgentAdapter`

The generic execution flow remains:

`AgentInstallation` -> `AgentVersion.runtime_handler` -> `AgentRuntimeRegistry` -> concrete adapter

The Prospecting Agent owns its own implementation boundary:

- `ProspectingAgentAdapter`
- `ProspectingRuntimeConfiguration`
- `ProspectingConfigurationResolver`

Agent-specific configuration validation continues to use the extensible validation registry introduced in Phase 3. The generic core does not add `if/elif` branches for concrete agents in the execution endpoint, project model, tenant model, or generic application services.

## Consequences

The platform can now host at least two concrete runtimes at the same time:

- Lívia for conversational sales
- Prospecting Agent for future commercial prospecting workflows

This proves that new agents should be introduced through ports, adapters, runtime handlers, and per-agent configuration contracts rather than by modifying the generic control flow.
