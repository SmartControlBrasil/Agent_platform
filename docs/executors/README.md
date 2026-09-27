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
