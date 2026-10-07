# Executors no monorepo Agent Platform

A raiz oficial do produto e `/home/marcelo/projetos/Agent_platform`. Backend Django, portal operacional, prospecção e integrações vivem na raiz do repositório.

Os **executors** são componentes de runtime separados (extensão Chrome, workers futuros), mas **não** são repositórios raiz independentes. O código versionado fica sob `executors/`.

## Chrome Executor

| Item | Caminho |
|------|---------|
| Código | `executors/chrome/` |
| Manifest | `executors/chrome/manifest.json` |
| Testes Node | `cd executors/chrome && npm test` |
| Pairing / API | `docs/executors/browser-extension-pairing.md` |
| Google Maps tool | `executors/chrome/docs/tools/google-maps-search.md` |

### Carregar em desenvolvimento

1. Abra `chrome://extensions/`.
2. Ative **Modo do desenvolvedor**.
3. **Carregar sem compactação** → selecione o diretório `executors/chrome/` dentro deste clone (caminho absoluto típico: `/home/marcelo/projetos/Agent_platform/executors/chrome`).
4. Configure a URL do Agent Platform na página de opções da extensão e conclua o pairing no popup.

Credenciais `AgentExecutor` (`aep_`) permanecem em `chrome.storage.local` do perfil; não entram no Git.


## Operational sweep for stale delegated executions

Delegated Maps searches can expire if no executor claims them before `expires_at`. Operate the sweep outside Django (systemd timer/cron), for example:

```bash
cd /home/marcelo/projetos/Agent_platform
python manage.py expire_tool_executions --tenant smart-control-brasil
```

Use `--dry-run` during validation. After the sweep, Hando shows the SearchRun as recoverable; operators can retry terminal expired attempts or redispatch a still non-terminal attempt without creating duplicate execution attempts.

## Hando tenant-scoped executor management

The Hando operations portal exposes tenant-local executor management at `/painel/configuracoes/executores/`. This surface is intentionally not a replacement for the Control Plane `/platform/executor-pairings/` flow. Hando resolves the active tenant from the authenticated portal session and never accepts a tenant selector for the pairing action.

Hando lists only `ToolExecutor` records where `executor.tenant` is the active tenant. Presence uses the shared `executor_presence()` rules based on `is_active` and `last_seen_at`, and the page shows enabled capabilities, latest heartbeat, latest execution, and `public_id` without exposing credentials or secrets.

Pending `ToolExecutorPairingRequest` rows are not listed in Hando because unclaimed requests are global until approved. A tenant manager/admin must type the public pairing code shown by the browser extension. The server validates that the code exists, is still pending, is not expired, and is either unassigned or already assigned to the active tenant. A successful claim calls the existing approval flow, associates the executor with the active tenant only, enables the default `prospecting.search_google_maps` capability, and leaves credential issuance to the existing extension consume endpoint.

Viewer and operator roles may view tenant executors through `tenant.view`. Pairing requires `tenant.manage`, so managers and tenant admins can bind an executor while lower-privilege roles cannot mutate executor identity. Control Plane pairing approval and rejection remain available for administrative/global operations.

