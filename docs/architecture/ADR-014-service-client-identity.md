# ADR-014: Service Client Identity

## Status

Accepted.

## Context

Agent Platform needs service-to-service access for client products such as `smart_sales`. These clients call Agent Platform over HTTP to execute authorized agents and tools, then poll for delegated execution results.

Existing identities are not appropriate for this role:

- Django `User` represents humans and Control Plane sessions.
- `ToolExecutor` represents workers such as Chrome extensions that claim and complete delegated tool executions.
- `Agent` and `AgentInstallation` represent runtime definitions and tenant/project-scoped deployments.

## Decision

Introduce `ServiceClient` as a distinct tenant-scoped machine identity with separate hashed credentials using the explicit `AgentClient` authorization scheme.

A `ServiceClient` receives no implicit tenant-wide permissions. It may use only `AgentInstallation` records granted through `ServiceClientAgentAccess`. Tool execution reuses the existing `execute_tool` application service, and delegated tools return a `tool_execution_id` for polling.

`ToolExecution.requested_by_service_client` records source attribution without changing executor ownership. A Chrome extension remains a `ToolExecutor`; `smart_sales` is a `ServiceClient`. They may belong to the same organization, but they do not share credentials or identity.

## Consequences

- Service-to-service APIs can be secured independently from human sessions and executor APIs.
- Least privilege is explicit per installation.
- Revocation and rotation are isolated to the client credential.
- Polling is sufficient for the first integration; callbacks/webhooks remain future work.
