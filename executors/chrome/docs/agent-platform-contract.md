# Agent Platform Contract

Versao minima esperada: Agent Platform Fase 7+.

Base URL configuravel pelo usuario, aceitando apenas `http` ou `https`.

## Pairing

- `POST /api/v1/executors/pairing/request/`
- `GET /api/v1/executors/pairing/{id}/status/`
- `POST /api/v1/executors/pairing/{id}/consume/`

A extensao nunca envia `tenant_id`; o tenant e associado no Control Plane.

## Executor Auth

Chamadas autenticadas enviam:

```http
Authorization: AgentExecutor <credential>
```

A extensao nunca envia `executor_id` para autenticar.

## Runtime

- `GET /api/v1/executors/me/`
- `POST /api/v1/executors/heartbeat/`
- `GET /api/v1/executors/tool-executions/`
- `POST /api/v1/executors/tool-executions/{id}/claim/`
- `POST /api/v1/executors/tool-executions/{id}/complete/`
- `POST /api/v1/executors/tool-executions/{id}/fail/`

A fila deve retornar somente execucoes delegadas, despachadas, tenant-scoped e compativeis com capabilities.
