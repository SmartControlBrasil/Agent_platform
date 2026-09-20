# ADR-001: Agent Platform

Agent Platform e o sistema central multi-tenant para administrar e executar agentes.

A copia inicial preserva a Livia como primeiro agente, mantendo compatibilidade com fluxos legados. O produto passa a ter identidade `agent_platform`, enquanto referencias `LIVIA_*` continuam como compatibilidade especifica da Livia.

Decisao: evoluir incrementalmente, sem reescrita total, por bounded contexts e adapters.
