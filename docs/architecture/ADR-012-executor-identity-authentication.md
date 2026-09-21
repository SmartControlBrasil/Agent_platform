# ADR-012: Executor Identity And Authentication

Status: accepted

## Context

`ToolExecutor` is an operational identity for delegated tool execution. Its `public_id` is a public identifier only and is never sufficient for authentication.

## Decision

Executor credentials use the format `aep_<prefix>_<secret>`. The platform stores only `credential_prefix` and a Django password hash of the full credential via `make_password`; verification uses `check_password`. Plaintext credentials are returned only once, at issuance.

Anonymous pairing requests do not accept a tenant id. A request creates a short-lived `PENDING` `ToolExecutorPairingRequest` with a random code and no tenant. A human approver in the Control Plane assigns the tenant during approval. This avoids trusting an anonymous client to claim tenant access.

Approval creates the `ToolExecutor` and marks the request `APPROVED`. Consumption by the client mints the credential and atomically marks the request `CONSUMED`. This differs slightly from a credential-at-approval flow so the platform never stores plaintext or needs a secret vault for later retrieval.

Executor authentication is separate from human authentication. Executor APIs use `Authorization: AgentExecutor <credential>` (Bearer is accepted for compatibility) and create an `ExecutorPrincipal` with `executor_id`, `tenant_id`, `credential_id`, and `executor_type`.

## Scope

Authenticated executors can access only their tenant, only active delegated executions, and only tools enabled through `ToolExecutorCapability`. Claim, complete, and fail use the authenticated executor; request bodies cannot pick arbitrary executor ids.

Credentials support revocation, expiration, last-used tracking, and simple rotation. Rotation revokes the previous credential and returns the new secret once.

## Audit

The platform records pairing, executor creation, credential creation/rotation/revocation, auth failures, and heartbeat events. Credential secrets are never included in audit metadata.

## Future Chrome Flow

A Chrome Extension will request pairing, show the pairing code to a human, poll status, consume once after approval, store the credential locally, send heartbeat, poll `/api/v1/executors/tool-executions/`, claim explicitly, and complete or fail executions.
