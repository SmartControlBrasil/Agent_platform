# ADR-003: AgentDefinition, AgentVersion e AgentInstallation

`AgentDefinition` representa um tipo de agente disponivel na plataforma.

`AgentVersion` representa uma versao executavel de um agente. O campo `runtime_handler` e uma chave controlada pelo registry, nao uma classe Python arbitraria.

`AgentInstallation` representa um agente instalado em um `Project` de um `Tenant`. A instalacao valida:

- o project pertence ao mesmo tenant;
- a versao pertence ao AgentDefinition informado;
- configuration nao armazena secrets em texto puro por chaves obvias.
