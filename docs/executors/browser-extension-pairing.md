# Browser Extension Pairing Contract

Phase 8 extension clients should use this contract.

1. Request pairing: `POST /api/v1/executors/pairing/request/`

```json
{"executor_type":"BROWSER_EXTENSION","requested_name":"Marcelo Chrome Extension"}
```

The response contains `id`, `pairing_code`, `status`, and `expires_at`. The client must show the code to the human operator.

2. Poll status: `GET /api/v1/executors/pairing/{id}/status/`

Statuses are `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`, and `CONSUMED`.

3. Consume once: `POST /api/v1/executors/pairing/{id}/consume/`

```json
{"pairing_code":"ABC123XYZ789"}
```

The response returns `credential` exactly once. Store it securely client-side. It cannot be retrieved again.

4. Authenticate executor calls with:

```http
Authorization: AgentExecutor aep_<prefix>_<secret>
```

5. Operational endpoints:

- `GET /api/v1/executors/me/`
- `POST /api/v1/executors/heartbeat/`
- `GET /api/v1/executors/tool-executions/`
- `POST /api/v1/executors/tool-executions/{id}/claim/`
- `POST /api/v1/executors/tool-executions/{id}/complete/`
- `POST /api/v1/executors/tool-executions/{id}/fail/`

The queue endpoint returns only tenant-scoped, dispatched, unexpired delegated executions compatible with the executor capabilities. GET never claims automatically.
