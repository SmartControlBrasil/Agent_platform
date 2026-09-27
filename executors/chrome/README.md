# Agent Executor Chrome

Executor de browser do Agent Platform, implementado como Chrome Extension Manifest V3.

Codigo versionado no monorepo **Agent Platform** em `executors/chrome/`. A separacao e arquitetural/runtime (extensao Chrome vs Django); a raiz oficial do projeto e `/home/marcelo/projetos/Agent_platform`.

Fluxo principal:

Install -> Configure URL -> Pair -> Admin approves -> Credential -> Heartbeat -> Poll -> Claim -> Execute -> Complete

Este componente nao e o backend Django e nao e `smart_sales`. Ele executa tools delegadas de browser, incluindo `prospecting.external_search_probe` e `prospecting.search_google_maps`, sem Google Maps API, CRM, leads, WhatsApp, IA, stealth ou CAPTCHA bypass.

## Desenvolvimento

```bash
npm test
```

Carregamento manual para desenvolvimento:

1. Abra `chrome://extensions`.
2. Ative Developer mode.
3. Clique em Load unpacked.
4. Selecione o diretorio `executors/chrome/` deste repositorio (ex.: `/home/marcelo/projetos/Agent_platform/executors/chrome`).
5. Configure a URL do Agent Platform na options page.
6. Solicite pairing no popup.

## Stack

- Chrome Extension Manifest V3
- JavaScript modules sem build
- Service worker
- `chrome.storage.local`
- `chrome.alarms`
- `fetch` com timeout via `AbortController`
- Testes unitarios com `node:test`

Host access for Agent Platform is requested as a restricted optional permission for local development and SmartControl origins. Google Maps execution uses explicit Maps host permissions and the executor capability `prospecting.search_google_maps`.

## Google Maps Tool

See `docs/tools/google-maps-search.md` for architecture, permissions, input/output contract, DOM strategy, privacy limits, and manual test flow. The backend must grant executor capability `prospecting.search_google_maps` before the queue will expose these executions.

For automated acceptance, use the already validated CDP loading path with `Extensions.loadUnpacked` and the existing executor configuration/flag. Do not use `--load-extension` as the operational acceptance criterion.
