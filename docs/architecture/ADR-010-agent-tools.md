# ADR-010: Agent Tools and Capabilities

Status: Accepted

## Context

The platform now supports multiple agent runtimes, but agents need explicit, reusable capabilities without coupling each agent adapter to concrete infrastructure helpers. Tool access must be tenant/project scoped, auditable, deterministic in tests, and never granted implicitly.

## Decision

Introduce a `tools` bounded context with:

- `ToolDefinition`: catalog entry for an available tool and its static `runtime_handler`.
- `AgentToolBinding`: explicit authorization that binds one tool to one `AgentInstallation`.
- `ToolRuntimePort`: runtime contract for local tool handlers.
- `ToolRuntimeRegistry`: static registry with no dynamic imports.
- `execute_tool`: central service that validates tenant, project, installation, binding, enabled state, active tool definition, executes the registered handler, and records `tool.executed`.

Control Plane manages bindings and records `tool.bound`, `tool.enabled`, `tool.disabled`, and `tool.configuration.updated`.

## Consequences

Agents do not call concrete tool adapters directly. The Prospecting Agent uses `execute_tool` for `prospecting.build_search_plan`; without an active binding it returns a controlled `tool_unavailable` state. The Lívia runtime remains unchanged and does not require tool bindings.

Adding new tools requires a model migration or catalog seed plus explicit registration in `tools.infrastructure.registry`. This keeps production behavior reviewable and avoids dynamic import surprises.
