# ADR-006: smart_sales Boundary

`smart_sales` e produto independente e nao sera importado como modulo Python da Agent Platform.

Integracao validada para a vertical slice de prospeccao:

```text
smart_sales
    -> HTTP AgentClient
Agent Platform
    -> ToolExecution / queue
Agent_executor_chrome
    -> Google Maps
Agent Platform
    -> smart_sales SearchResult
```

Podem existir agentes usados pelo `smart_sales`, como prospecting, qualification e followup. O produto `smart_sales` permanece fora deste repositorio e fora do runtime Python da plataforma.

Agent Platform autentica o produto consumidor como `AgentClient` (`apc_`) e autentica executores delegados como `AgentExecutor` (`aep_`). Esses papeis sao separados: `smart_sales` nao e `ToolExecutor`, e o Chrome Executor nao usa credencial de cliente.

O codigo do Chrome Executor nao reside neste repositorio; ele e mantido separadamente em `Agent_executor_chrome`. Agent Platform mantem o contrato, a fila, o claim e o complete/fail de `ToolExecution`, mas nao contem scraper/browser do Google Maps.
