# ADR-007: Agent Platform Control Plane

## Status

Accepted.

## Context

The Agent Platform needs a web administrative surface for the concepts introduced by the multi-agent baseline without turning Django Admin into the product interface and without changing the Lívia legacy runtime.

## Decision

Create a Django server-rendered Control Plane at `/platform/`.

The Control Plane is not Django Admin, not an Agent Runtime, and not the `smart_sales` domain. It administers platform resources:

- Tenant
- Project
- AgentDefinition
- AgentVersion
- AgentInstallation

Views are authenticated and tenant-scoped through `TenantMembership` and the existing capability model. Read operations use tenant view access. Mutating operations such as project creation, agent installation, enable/disable, and configuration updates require tenant management access.

Agent installation, enable, disable, and configuration updates go through application services so business invariants do not live directly in views.

## Consequences

The first catalog entry is Lívia, represented as an `AgentDefinition`. The runtime handler remains read-only in the UI and stays a technical configuration.

`AgentInstallation.configuration` is used for non-secret platform configuration fields only. It does not replace `AssistantProfile` in this phase, and these fields are documented in the UI as not yet substituting the legacy runtime profile.

Existing APIs remain unchanged.
