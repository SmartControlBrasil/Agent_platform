# ADR-008: Lívia Runtime Configuration from Agent Installation

## Status

Accepted.

## Context

The Agent Platform now has `AgentInstallation` as the modern unit that connects a tenant project to an installed agent version. Lívia still has a legacy `AssistantProfile` that powers the public widget and existing chat flows.

Phase 3 needs the modern execution path to consume `AgentInstallation.configuration` without removing `AssistantProfile`, without changing the generic agent domain to know about Lívia, and without treating Lívia as a tenant singleton.

## Decision

Lívia-specific configuration is resolved inside the Lívia implementation boundary.

The generic agent domain and application layer remain free of Lívia-specific conditionals. The runtime registry still resolves the `livia` handler, and `LiviaAgentAdapter` receives an already resolved `AgentExecutionContext` containing `tenant_id`, `project_id`, and `installation_id`.

`LiviaConfigurationResolver` produces a `LiviaRuntimeConfiguration` value object with these initial fields:

- `public_name`
- `tone`
- `primary_goal`
- `short_description`

Precedence is:

1. `AgentInstallation.configuration` for known Lívia fields.
2. Active legacy `AssistantProfile` for the tenant.
3. Safe defaults.

The resolver validates only Lívia-known fields. Unknown fields are ignored by the Lívia resolver so future agents or future versions can add their own configuration contracts without a global schema migration.

## Compatibility

Legacy callers continue to call `process_chat_request` without installation context, so they keep using `AssistantProfile` directly.

Modern execution through `/api/v1/agent-installations/{id}/execute/` loads the exact installation from `AgentExecutionContext.installation_id` and `project_id`; it does not pick the first Lívia installation in a tenant.

The adapter builds an effective profile-like object at the boundary and passes it into the legacy pipeline through `assistant_profile_override`. The legacy pipeline does not query `AgentInstallation` directly.

## Versioning

Configuration belongs to the installation and is expected to be compatible with the installed `AgentVersion`. Automatic configuration migration across versions is not implemented in this phase.

## Consequences

A single tenant can run multiple Lívia installations in different projects with different names and behavior hints, while still sharing the legacy tenant-level `AssistantProfile` as fallback.

`AssistantProfile` remains a legacy compatibility object for Lívia and the widget. New generic Agent Platform features should not be added to it.

Audit events for configuration updates should identify the installation and changed field names without storing secret or unnecessary full values.
