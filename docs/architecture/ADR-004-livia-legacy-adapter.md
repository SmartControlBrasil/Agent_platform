# ADR-004: Livia Legacy Adapter

A Livia nao foi reescrita nesta fase.

Criamos `LiviaAgentAdapter`, que implementa o contrato de runtime e chama o pipeline legado `process_chat_request`. A plataforma passa a depender do `AgentRuntimePort`, nao de prompts, qualification, discovery ou detalhes do widget da Livia.

`AssistantProfile` permanece como compatibilidade legada e nao deve receber novas responsabilidades genericas de multiagente. A migracao futura sera:

AssistantProfile -> AgentInstallation.configuration
