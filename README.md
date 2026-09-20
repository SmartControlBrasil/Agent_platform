# Agent Platform

Plataforma central multi-tenant para execucao e administracao de agentes.

Modelo inicial:

Tenant -> Project -> AgentInstallation -> AgentDefinition + AgentVersion -> Agent Runtime
                              -> AgentToolBinding -> ToolDefinition -> Tool Runtime

Agentes disponiveis:

- Livia
  - `conversational_sales`
- Prospecting Agent
  - `sales_prospecting`

A Livia permanece como implementacao legada compativel e e executada por um adapter (`LiviaAgentAdapter`) atras do `AgentRuntimePort`. O Prospecting Agent e o segundo runtime real da plataforma e valida a extensibilidade do registry sem acoplamento com `smart_sales`.

Produtos clientes futuros podem consumir a plataforma por HTTP/API/eventos, incluindo `smart_sales`, websites e outros sistemas. `smart_sales` e um produto cliente futuro, nao o Prospecting Agent em si. O projeto original da Livia continua independente em producao e nao deve ser alterado a partir deste repositorio.

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
- `GET /api/v1/tools/`
- `GET /api/v1/agent-installations/{id}/tools/`
- `POST /api/v1/tool-bindings/{id}/execute/`

As listas e execucao exigem usuario autenticado e respeitam o escopo de tenant via `TenantMembership`.

## Configuracao efetiva da Livia

Na Fase 3, `AgentInstallation` passa a ser a configuracao moderna consumida pelo runtime da Livia.

Precedencia da configuracao efetiva:

1. `AgentInstallation.configuration` para campos conhecidos da Livia
2. `AssistantProfile` legado ativo do tenant
3. defaults seguros

O caminho moderno usa a `installation` exata do contexto de execucao. O widget e os fluxos legados continuam funcionando via `AssistantProfile`, sem buscar `AgentInstallation` diretamente.


## Tools e capacidades

Na Fase 5, ferramentas passam a ser capacidades explicitas de uma instalacao de agente. Uma tool so pode ser executada quando existe um `AgentToolBinding` ativo para a `AgentInstallation` exata, dentro do mesmo tenant e projeto.

A primeira tool e `prospecting.build_search_plan`, usada pelo Prospecting Agent para montar um plano deterministico de consultas de prospeccao. Sem binding ativo, o Prospecting Agent retorna `tool_unavailable` e nao concede acesso automaticamente. A Livia permanece sem dependencia de tools.
