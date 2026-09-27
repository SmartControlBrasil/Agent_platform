# Manual Integration Checklist

1. Agent Platform local rodando.
2. Carregar extensao unpacked no Chrome.
3. Configurar URL, por exemplo `http://127.0.0.1:8000`.
4. Solicitar pairing no popup.
5. Verificar pairing no Control Plane.
6. Aprovar pairing.
7. Confirmar que a extensao consome a credential.
8. Confirmar que `/me` funciona.
9. Confirmar heartbeat e `last_seen_at` no Control Plane.
10. Criar uma execucao delegada `prospecting.external_search_probe`.
11. Aguardar polling da extensao.
12. Confirmar claim.
13. Confirmar execucao local do probe.
14. Confirmar complete.
15. Verificar `SUCCEEDED` no Control Plane.


## Google Maps Search Manual Check

1. Agent Platform local no commit com `prospecting.search_google_maps` registrado.
2. Instalar Prospecting Agent em um projeto.
3. Adicionar binding `prospecting.search_google_maps` pela UI.
4. Parear Chrome executor.
5. Adicionar capability `prospecting.search_google_maps` ao executor no Control Plane.
6. Criar ToolExecution delegada com input: `queries=["hospital privado Barueri"]`, `max_results=10`, `locale="pt-BR"`.
7. Aguardar polling normal ou, em desenvolvimento, chamar `chrome.runtime.sendMessage({ type: 'AGENT_EXECUTOR_DEV_POLL_NOW' })` no contexto da extensão.
8. Confirmar fluxo DISPATCHED -> RUNNING -> SUCCEEDED.
9. Confirmar `businesses.length <= max_results`.
10. Conferir manualmente alguns nomes, endereços, sites e telefones quando presentes.
11. Confirmar que `phone=null` e `website=null` sao aceitos quando ausentes.
12. Repetir com queries proximas para validar dedupe.
13. Se challenge aparecer naturalmente, confirmar fail seguro `google_challenge`; nao tentar resolver automaticamente.


## Smart Sales E2E Acceptance Check

This check validates the real chain `smart_sales -> Agent Platform -> executors/chrome -> Google Maps -> Agent Platform -> smart_sales` without adding CRM, Prospect promotion, email automation, crawler behavior, a new parser, or AI.

Preconditions:

- Agent Platform is running and exposes `prospecting.search_google_maps`.
- `smart_sales` has an active Agent Platform connection for the same tenant.
- The product connection uses `AgentClient` credentials (`apc_`).
- This executor is paired, online, and authenticated as `AgentExecutor` (`aep_`).
- The executor capability `prospecting.search_google_maps` is enabled.
- The Chrome extension is loaded through the validated CDP path `Extensions.loadUnpacked` when running automated acceptance. Manual `Load unpacked` remains fine for local exploratory development.

Flow:

1. Dispatch a `SearchRun` from `smart_sales`.
2. Confirm the Agent Platform `ToolExecution` is `DISPATCHED`.
3. Confirm the executor queue returns that exact execution.
4. Confirm this executor claims it.
5. Confirm status becomes `RUNNING`.
6. Let the browser execute the Google Maps workflow.
7. Confirm complete returns `SUCCEEDED`.
8. Refresh the `SearchRun` in `smart_sales`.
9. Confirm `SearchRun.COMPLETED` and expected `SearchResults`.
10. Repeat refresh and confirm no duplicate `SearchResults`.
11. Redispatch the same logical run and confirm idempotency preserves the existing execution.

Security checks:

- Do not log or persist `AgentClient` credentials, `AgentExecutor` credentials, or `Authorization` headers.
- Result payloads must not contain `apc_`, `aep_`, cookies, raw HTML, storage dumps, or backend secrets.
- The content script must remain isolated from credentials and must not call Agent Platform directly.
