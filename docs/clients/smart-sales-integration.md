# ServiceClient Integration Contract

This document uses `smart_sales` as the first expected client, but the protocol is generic for any Agent Platform `ServiceClient`.

## Identity

Service clients authenticate as machine clients with:

```http
Authorization: AgentClient <credential>
```

A `ServiceClient` is not a Django user, not a `ToolExecutor`, not an agent, and not an `AgentInstallation`. Credentials are issued once, stored only as hashes, and may be rotated or revoked in the Control Plane.

## Metadata

```http
GET /api/v1/clients/me/
Authorization: AgentClient apc_<prefix>_<secret>
```

Returns safe client metadata, tenant id, slug, active flag, and credential id. It never returns secrets.

## Execute A Tool

```http
POST /api/v1/clients/agent-installations/{installation_id}/tools/{tool_slug}/execute/
Authorization: AgentClient apc_<prefix>_<secret>
Content-Type: application/json
```

Body:

```json
{
  "input": {},
  "idempotency_key": "smart_sales:search_run:<uuid>"
}
```

The client must have explicit `ServiceClientAgentAccess` for the installation. Tenant scope is derived from the credential; clients cannot choose a tenant.

For delegated tools such as `prospecting.search_google_maps`, the initial response is:

```json
{
  "status": "dispatched",
  "tool_execution_id": "UUID"
}
```

Repeated requests with the same `idempotency_key` in the same installation/tool context return the same `tool_execution_id` and do not create a second search.

## Google Maps Example

```json
{
  "input": {
    "schema_version": 1,
    "queries": ["hospital privado Barueri"],
    "target_region": "Barueri - SP",
    "max_results": 10,
    "locale": "pt-BR"
  },
  "idempotency_key": "smart_sales:search_run:8f4f1d4c"
}
```

## Poll Execution Status

```http
GET /api/v1/clients/tool-executions/{tool_execution_id}/
Authorization: AgentClient apc_<prefix>_<secret>
```

Response while queued/running:

```json
{
  "id": "UUID",
  "tool": {"slug": "prospecting.search_google_maps"},
  "status": "DISPATCHED",
  "created_at": "...",
  "started_at": null,
  "completed_at": null
}
```

When succeeded, `result` is included. When failed, sanitized `error_code` and `error_message` are included.

## Security Rules

Clients can only access executions for installations they are explicitly authorized to use. Revoked or expired credentials fail authentication. Human browser sessions do not authenticate these machine endpoints.
