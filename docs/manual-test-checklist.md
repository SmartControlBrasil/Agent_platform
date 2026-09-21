# Manual Integration Checklist

1. Agent Platform local rodando.
2. Carregar extensao unpacked no Chrome.
3. Configurar URL, por exemplo `http://127.0.0.1:8000`.
4. Solicitar pairing no popup.
5. Verificar pairing no Control Plane.
6. Aprovar pairing.
7. Confirmar que a extensao consome a credential.
8. Confirmar que `/me` funciona.
9. Confirmar heartbeat e `last_seen_at` no Control Plane.
10. Criar uma execucao delegada `prospecting.external_search_probe`.
11. Aguardar polling da extensao.
12. Confirmar claim.
13. Confirmar execucao local do probe.
14. Confirmar complete.
15. Verificar `SUCCEEDED` no Control Plane.
