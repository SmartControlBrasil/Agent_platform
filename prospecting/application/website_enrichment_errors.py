from django.core.exceptions import ValidationError


def website_enrichment_error_message(exc: ValidationError) -> str:
    text = "; ".join(getattr(exc, "messages", [str(exc)])).lower()
    if "blocked network" in text or "blocked" in text and "address" in text:
        return "Website bloqueado por política de segurança (SSRF)."
    if "could not be resolved" in text or "host could not" in text:
        return "Website indisponível (falha de DNS/resolução)."
    if "timed out" in text or "timeout" in text or "fetch failed" in text:
        return "Timeout ao acessar o website."
    if "redirected too many" in text:
        return "Website redirecionou demais."
    if "content type" in text:
        return "Website retornou conteúdo não suportado."
    if "too large" in text:
        return "Resposta do website excedeu o limite permitido."
    if "must use http" in text or "must include a host" in text:
        return "Website inválido."
    if "no website" in text:
        return "Prospect não possui website para enriquecimento."
    return "; ".join(getattr(exc, "messages", [str(exc)]))
