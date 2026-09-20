# ADR-002: Hexagonal Architecture e DDD

Adotamos DDD e arquitetura hexagonal como direcao, sem criar abstracoes vazias.

Regras:

- domain nao depende de Django, ORM, HTTP, OpenAI, Redis ou Celery;
- application coordena casos de uso e conhece ports;
- infrastructure contem ORM, providers externos, storage e adapters;
- interfaces expoem REST, web, admin, widget e commands.

O novo contrato `AgentRuntimePort` fica em `agents.domain.runtime`. O registry fica na camada application. A implementacao legada da Livia fica em infrastructure.
