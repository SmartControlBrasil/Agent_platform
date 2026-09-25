# Agent Executor Chrome

Executor de browser do Agent Platform, implementado como Chrome Extension Manifest V3.

Fluxo principal:

Install -> Configure URL -> Pair -> Admin approves -> Credential -> Heartbeat -> Poll -> Claim -> Execute -> Complete

Este projeto nao e o backend Agent Platform e nao e smart_sales. Ele executa tools delegadas de browser, incluindo `prospecting.external_search_probe` e `prospecting.search_google_maps`, sem Google Maps API, CRM, leads, WhatsApp, IA, stealth ou CAPTCHA bypass.

## Desenvolvimento

```bash
npm test
```

Carregamento manual para desenvolvimento:

1. Abra `chrome://extensions`.
2. Ative Developer mode.
3. Clique em Load unpacked.
4. Selecione este diretorio.
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
