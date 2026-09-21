# Agent Executor Chrome

Executor de browser do Agent Platform, implementado como Chrome Extension Manifest V3.

Fluxo principal:

Install -> Configure URL -> Pair -> Admin approves -> Credential -> Heartbeat -> Poll -> Claim -> Execute -> Complete

Este projeto nao e o backend Agent Platform, nao e smart_sales e ainda nao implementa Google Maps, scraping, WhatsApp, IA, CRM ou browser automation.

## Desenvolvimento

```bash
npm test
```

Carregamento manual:

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

Host access is requested as an optional host permission only for the configured Agent Platform origin.
