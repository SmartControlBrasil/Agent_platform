# ADR-006: smart_sales Boundary

`smart_sales` e produto independente e nao sera importado como modulo Python da Agent Platform.

Integracao futura:

smart_sales -> HTTP/API/events -> Agent Platform

Podem existir agentes usados pelo `smart_sales`, como prospecting, qualification e followup. O produto `smart_sales` permanece fora deste repositorio e fora do runtime Python da plataforma.
