# Security

## Credential Storage

O MVP armazena a credential em `chrome.storage.local`. A credential nunca fica no manifest, no source, em query string, em logs ou em DOM persistente. Popup e options nao exibem o segredo.

## Threat Model

A credential identifica um `ToolExecutor` e deve ser tratada como segredo bearer. Se a maquina ou perfil Chrome for comprometido, a credential pode ser extraida do storage local. O Control Plane deve permitir revogacao e rotacao.

## Least Privilege

Permissoes: `storage` e `alarms` para estado operacional; `tabs` e `scripting` para criar uma aba dedicada e injetar o content script do Google Maps. Host permissions permanentes ficam restritas a `https://www.google.com/maps*` e `https://www.google.com/maps/*`. A origem do Agent Platform continua sendo solicitada como optional host permission, limitada aos padroes local development e SmartControl declarados no manifest.

## Capabilities

Ter handler embutido nao significa autorizacao. O backend filtra fila por capabilities efetivas. A extensao tambem so executa slugs previamente registrados no dispatcher local.

## Remote Code Execution

Nao ha `eval`, `Function(string)`, import dinamico remoto ou script remoto. O servidor nunca envia JavaScript para executar.

## Revocation And Rotation

401/403 colocam a extensao em estado `REVOKED` e interrompem o ciclo operacional ate o usuario desconectar ou parear novamente.

O content script de Google Maps nao recebe credential, nao conhece tenant, nao chama Agent Platform e nao usa `fetch`; ele apenas responde mensagens internas whitelisted com dados publicos renderizados na pagina.
