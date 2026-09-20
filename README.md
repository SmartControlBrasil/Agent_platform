# Agent Platform

Plataforma central multi-tenant para execucao e administracao de agentes.

Modelo inicial:

Tenant -> Project -> AgentInstallation -> AgentDefinition + AgentVersion -> Agent Runtime

O primeiro agente registrado e a Livia. A Livia permanece como implementacao legada compativel e e executada por um adapter (`LiviaAgentAdapter`) atras do `AgentRuntimePort`.

Produtos clientes futuros podem consumir a plataforma por HTTP/API/eventos, incluindo `smart_sales`, websites e outros sistemas. O projeto original da Livia continua independente em producao e nao deve ser alterado a partir deste repositorio.

## Seguranca local

Use `.env.example` como base. Os defaults de desenvolvimento mantem integracoes externas desligadas:

- `EXTERNAL_INTEGRATIONS_ENABLED=false`
- `LIVIA_AI_ENABLED=false`
- `LIVIA_RAG_ENABLED=false`
- `LIVIA_WEBHOOKS_REAL_ENABLED=false`
- `SMART360_LEAD_DISPATCH_REAL_ENABLED=false`
- email via console backend

Nao armazene credenciais reais no repositorio.

## API inicial

- `GET /api/v1/platform/health/`
- `GET /api/v1/agents/`
- `GET /api/v1/projects/`
- `GET /api/v1/agent-installations/`
- `POST /api/v1/agent-installations/{id}/execute/`

As listas e execucao exigem usuario autenticado e respeitam o escopo de tenant via `TenantMembership`.

## Configuracao efetiva da Livia

Na Fase 3, `AgentInstallation` passa a ser a configuracao moderna consumida pelo runtime da Livia.

Precedencia da configuracao efetiva:

1. `AgentInstallation.configuration` para campos conhecidos da Livia
2. `AssistantProfile` legado ativo do tenant
3. defaults seguros

O caminho moderno usa a `installation` exata do contexto de execucao. O widget e os fluxos legados continuam funcionando via `AssistantProfile`, sem buscar `AgentInstallation` diretamente.
