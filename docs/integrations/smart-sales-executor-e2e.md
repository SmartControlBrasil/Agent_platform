# Smart Sales Executor E2E

Este documento consolida o E2E real validado entre `smart_sales`, Agent Platform e `Agent_executor_chrome` para a tool delegada `prospecting.search_google_maps`.

## Fluxo Validado

```text
smart_sales
    | AgentClient
    v
Agent Platform
    | ToolExecution / queue
    v
Agent_executor_chrome
    |
    v
Google Maps

Google Maps
    |
    v
Agent_executor_chrome
    | complete
    v
Agent Platform
    | poll/refresh
    v
smart_sales
    |
    v
SearchRun.COMPLETED + SearchResults
```

## Responsabilidades

`smart_sales` e o produto consumidor. Ele cria o `SearchRun`, resolve a conexao com Agent Platform, autentica como `AgentClient`, dispara a tool, persiste/reconcilia o `ToolExecution` externo, materializa `SearchResults` e preserva idempotencia no redispatch e no refresh.

Agent Platform e o control plane/gateway de execucao. Ele autentica `AgentClient`, valida tool/input, cria e mantem `ToolExecution`, controla tenant/capability, fornece a queue, controla claim e recebe complete/fail. Ele nao contem scraper nem browser do Google Maps.

`Agent_executor_chrome` autentica como `AgentExecutor`, envia heartbeat, consulta queue, faz claim, executa `prospecting.search_google_maps` no navegador real e envia o resultado para Agent Platform. Ele nao conhece regras comerciais do `smart_sales`.

## Identidades

`AgentClient` e a identidade server-to-server usada por produtos consumidores como `smart_sales`. Credenciais usam prefixo `apc_` e devem ser enviadas como `Authorization: AgentClient <credential>`.

`AgentExecutor` e a identidade operacional do executor. Credenciais usam prefixo `aep_` e devem ser enviadas como `Authorization: AgentExecutor <credential>`.

Os papeis nunca devem ser intercambiados: `smart_sales` nao e `ToolExecutor`, e o Chrome Executor nao usa credencial `AgentClient`.

## Preconditions De Acceptance

- Agent Platform disponivel.
- `ServiceClient` ativo para o tenant e autorizado na `AgentInstallation` correta.
- Chrome Executor pareado e com credencial `AgentExecutor`.
- Executor online com heartbeat recente.
- Binding da tool `prospecting.search_google_maps` ativo na instalacao do Prospecting Agent.
- Capability `prospecting.search_google_maps` ativa para o executor.
- Extensao Chrome carregada corretamente.
- `smart_sales`, Agent Platform e Chrome Executor apontando para o mesmo tenant/contexto.

## Runbook De Acceptance

1. Criar ou selecionar um `SearchRun` em `smart_sales`.
2. Executar o dispatch do `SearchRun`.
3. Identificar o `ToolExecution` externo retornado/persistido.
4. Confirmar status `DISPATCHED` no Agent Platform.
5. Confirmar que a queue do executor entrega a `ToolExecution` correta.
6. Confirmar claim pelo Chrome Executor.
7. Confirmar transicao para `RUNNING`.
8. Executar a busca real no Google Maps pelo Chrome Executor.
9. Confirmar complete com status `SUCCEEDED`.
10. Executar refresh/poll do `SearchRun` em `smart_sales`.
11. Confirmar `SearchRun.COMPLETED`.
12. Conferir `SearchResults` persistidos.
13. Repetir refresh e validar ausencia de duplicacao.
14. Executar redispatch da mesma operacao logica.
15. Validar reutilizacao da mesma `ToolExecution` por `idempotency_key`.

## Mecanismo Da Extensao

No acceptance automatizado real, nao use `--load-extension` como criterio operacional. O mecanismo validado carrega a extensao pelo Chrome DevTools Protocol com `Extensions.loadUnpacked`, usando a configuracao/flag ja existente no `Agent_executor_chrome`.

## Idempotencia

A mesma operacao logica nao deve criar multiplas `ToolExecutions`. Redispatch pode ocorrer sem trocar o execution id quando a `idempotency_key` e a mesma. Refresh repetido nao deve duplicar `SearchResult`. O resultado final `COMPLETED` deve permanecer estavel.

## Seguranca

Nunca registre ou persista credenciais `AgentClient`, credenciais `AgentExecutor` ou header `Authorization`. Payloads persistidos, logs, `SearchRun`, `SearchResult` e `result_payload` devem continuar livres de `apc_`, `aep_` e `Authorization`.

## Incidente De URL Longa

O E2E real encontrou um `maps_url` de 271 caracteres, maior que o `URLField` padrao do Django. A correcao no `smart_sales` ampliou `website` e `maps_url` para `max_length=1000` e adicionou teste de regressao para importacao de URL real longa. Esse problema apareceu apenas na execucao real de Google Maps.

## Evidencia Historica

A acceptance real da Fase 10B.1 validou uma execucao com 5 businesses retornados, `ToolExecution` finalizada como `SUCCEEDED`, `SearchRun` finalizado como `COMPLETED`, 5 `SearchResults` persistidos, refresh duplo sem duplicacao e redispatch preservando a mesma execucao. O UUID de acceptance pode ser usado como evidencia historica interna, mas nao deve virar configuracao obrigatoria de producao.
