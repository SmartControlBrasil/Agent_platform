# Baseline da Importacao da Livia

Data: 2026-09-20

## Stack encontrada

- Python: nao ha `.python-version` ou `pyproject.toml` no baseline auditado.
- Django: `Django==6.0.6`.
- Dependencias: `requirements.txt`.
- Banco: `DATABASE_URL` via `dj-database-url`; fallback SQLite local quando `DEBUG=true` ou em testes.
- AUTH_USER_MODEL: default do Django (`auth.User`), sem override encontrado.
- Apps instalados antes da mudanca: `tenants`, `assistant_core`, `conversations`, `leads`, `knowledge_base`, `widget`, `integrations`, `operations_portal`, `audit`.

## Entrypoints e endpoints legados

- `manage.py`
- `config.wsgi`
- `config.asgi`
- `/health/`
- `/api/chat/`
- `/install/<tenant>/`
- `/painel/`
- widget em rotas raiz via `widget.urls`

## Estrutura de tenants

`Tenant` possui `name`, `slug`, `domain`, `is_active` e timestamps. `AssistantProfile` e one-to-one com `Tenant` e permanece legado da Livia. `TenantMembership` controla acesso por papel.

## Integracoes externas encontradas

- OpenAI: `LIVIA_OPENAI_*`
- Google Drive/service account: `LIVIA_GOOGLE_*`
- RAG embeddings: `LIVIA_RAG_EMBEDDING_*`
- SMTP/email: `EMAIL_*`, notificacoes Livia
- Smart360: `SMART360_*`
- Webhooks/outbox: `LIVIA_WEBHOOKS_*`, `OutboxEvent`
- Operational monitoring/notifications: `LIVIA_OPERATIONAL_*`

## Neutralizacao inicial

`.env.example` mantem integracoes desligadas e sem credenciais. `EXTERNAL_INTEGRATIONS_ENABLED=false` e o default. O backend de email padrao em desenvolvimento passa a console backend quando integracoes externas nao estao habilitadas.

## Evolucoes documentadas

- Conversations futuramente recebera `project` e `agent_installation` nullable.
- RAG futuramente deve evoluir de Tenant -> Knowledge/RAG para KnowledgeBase/KnowledgeBinding por Project e AgentInstallation.
- `AiUsageEvent` deve ganhar contexto de project/agente sem quebrar registros existentes.
