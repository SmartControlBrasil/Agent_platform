# ADR-005: Multitenancy

Isolamento por tenant e obrigatorio.

`Project` pertence a um `Tenant`; `AgentInstallation` tambem carrega `tenant` e `project`. A API filtra objetos pelos tenants acessiveis ao usuario atraves de `TenantMembership`.

Referencias futuras em `Conversation` devem ser adicionadas de forma nullable e compativel:

- project nullable;
- agent_installation nullable.

Essa alteracao nao foi feita nesta fase para evitar impacto prematuro nos fluxos legados.
