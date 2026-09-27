# Índice documental de fases — Lívia Platform

Este índice elimina ambiguidade entre trilhas paralelas que reutilizam números de fase.

## Trilha operacional do portal (canônica Fases 10–18)

| Fase | Tema | Documentos principais |
|------|------|----------------------|
| 10 | Observabilidade RAG/IA | `phase10_*`, `phase10_rag_ai_observability.md` |
| 11 | Alertas operacionais | `phase11_*`, `phase11_operational_alerts.md` |
| 12 | Monitoramento | `phase12_*`, `phase12_operational_monitoring.md` |
| 13 | Governança (silences, maintenance) | `phase13_*`, `phase13_operational_governance.md` |
| 14 | Fila operacional / SLA | `phase14_*`, `phase14_operational_work_queue.md` |
| 15 | Notificações | `phase15_*`, `phase15_operational_notifications.md` |
| 16 | Analytics operacional | `phase16_*`, `phase16_operational_analytics.md` |
| 17 | Validação PostgreSQL / pgvector | `phase17_*`, `deploy/sqlite_to_postgresql.md` |
| 18 | Reconciliação migrations / commits | `phase18_*` |

## Trilha RAG / grounded (numeração antiga)

Documentos como `phase16_rag_validation_report.md`, `phase17_staging_run_report.md` referem-se a **soak/staging RAG**, não à trilha operacional acima. Consultar cabeçalho de cada doc para contexto.

## Trilha staging / deploy

- `docs/deploy/staging_physical_deployment.md`
- `docs/deploy/sqlite_to_postgresql.md`
- `deploy/staging/*.service`, `*.timer`

## Trilha PostgreSQL readiness

Branch: `chore/postgresql-readiness`
HEAD publicado até Fase 10–16 parcial: `9fd5d38`

## Trilha prospecção comercial — FASE 15A (Minha Fila)

Numeração **15A** distinta da Fase 15 operacional/KB (notificações). Não renumerar a trilha 10–18.

| Subfase | Entrega |
|---------|---------|
| **15A.1** | Motor `prospecting/application/commercial_queue.py`, testes de domínio |
| **15A.2** | Hando `/painel/prospeccao/minha-fila/`, sidebar, testes portal |
| **15A.3** | Dashboard, docs, hardening performance/UX |

Documentação: `docs/agents/prospecting.md`, seção Minha Fila em `docs/operations_portal.md`.

## Trilha integração de sites (Fase 24)

- `docs/site_integration.md` — onboarding, readiness, snippet e política de origins
- `docs/tenants/granimarmores_pitondo_installation.md` — runbook operacional do primeiro tenant comercial
- `docs/external_side_effect_safety.md` — gates de side effects externos e smoke seguro
- `docs/widget_installation.md` — pacote `/install/<slug>/`
- `docs/widget_origin_security.md` — validação de origin/CORS

## Checkpoint comercial (Fases 24–27)

- **Fase 24 — Site Integration Readiness**: contrato de instalação, readiness por tenant, `/install/<slug>/`, snippet oficial e testes focados.
- **Fase 25 — Granimármores Tenant Preparation**: preparação idempotente do tenant `granimarmores-pitondo`, origins oficiais e runbook comercial.
- **Fase 26 — Safe Commercial Smoke**: política central fail-closed de side effects, comando de readiness de side effects e smoke comercial com rollback.
- **Fase 27 — Commercial Checkpoint**: auditoria final, validação proporcional e commit único de checkpoint comercial local.
