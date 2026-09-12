from __future__ import annotations

import re
from dataclasses import dataclass

from assistant_core.discovery import analyze_message
from conversations.models import Message


@dataclass(frozen=True)
class ConversationSummary:
    title: str
    need_summary: str
    service_area: str
    intent: str
    urgency: str
    products_or_services: tuple[str, ...]
    visitor_context: str
    collected_fields: tuple[str, ...]
    conversation_notes: tuple[str, ...]
    recommended_next_step: str
    source_page: str
    tenant_slug: str


AREA_LABELS = {
    "support": "suporte",
    "unknown": "indefinido",
}

GENERIC_PRODUCT_STOPWORDS = {
    "atendimento", "cidade", "contato", "empresa", "gostaria", "mensagem", "nome",
    "orcamento", "orçamento", "preciso", "proposta", "quero", "sobre", "telefone",
    "com", "minha", "meu", "visita", "tecnica", "técnica",
}

def build_conversation_summary(conversation, lead_draft=None) -> ConversationSummary:
    messages = _conversation_messages(conversation)
    user_messages = _substantive_user_messages(messages)
    corpus = _build_corpus(user_messages, lead_draft)
    discovery = analyze_message(corpus)
    service_area = _resolve_service_area(discovery.service_area, corpus)
    need_summary = _resolve_need_summary(lead_draft, user_messages)
    products = _extract_products_or_services(corpus)
    collected_fields = _collected_fields(lead_draft)
    urgency = _detect_urgency(corpus, service_area)
    notes = _conversation_notes(user_messages)
    return ConversationSummary(
        title=_build_title(service_area, discovery.intent),
        need_summary=need_summary,
        service_area=service_area,
        intent=discovery.intent,
        urgency=urgency,
        products_or_services=products,
        visitor_context=_build_visitor_context(lead_draft),
        collected_fields=collected_fields,
        conversation_notes=notes,
        recommended_next_step=_recommended_next_step(discovery.intent, service_area, urgency, corpus),
        source_page=str(getattr(conversation, "source_page", "") or "").strip(),
        tenant_slug=str(getattr(getattr(conversation, "tenant", None), "slug", "") or "").strip(),
    )


def format_conversation_summary_notes(summary: ConversationSummary) -> str:
    collected = ", ".join(summary.collected_fields) if summary.collected_fields else "nenhum dado validado ainda"
    products = ", ".join(summary.products_or_services) if summary.products_or_services else "não identificado"
    origin_parts = [summary.tenant_slug or "tenant indefinido"]
    if summary.source_page:
        origin_parts.append(summary.source_page)

    lines = [
        "Resumo da Lívia:",
        f"- Interesse: {AREA_LABELS.get(summary.service_area, summary.service_area or 'indefinido')}",
        f"- Necessidade: {summary.need_summary}",
        f"- Produtos/serviços citados: {products}",
        f"- Urgência: {summary.urgency}",
        f"- Dados coletados: {collected}",
        f"- Origem: {' | '.join(origin_parts)}",
        "- Conversa:",
    ]
    if summary.conversation_notes:
        lines.extend(f"  - {note}" for note in summary.conversation_notes)
    else:
        lines.append("  - Sem histórico detalhado registrado.")
    lines.append(f"- Próximo passo sugerido: {summary.recommended_next_step}")
    return "\n".join(lines)


MAX_TRANSCRIPT_CHARS = 14000
MAX_TRANSCRIPT_TURNS = 80
NOTIFICATION_TOPIC_PATTERNS = (
    (re.compile(r"rob[oô]", re.IGNORECASE), "Robô de limpeza"),
    (re.compile(r"ar[- ]?condicionado", re.IGNORECASE), "Ar-condicionado"),
    (re.compile(r"automa", re.IGNORECASE), "Automação"),
    (re.compile(r"\bclp\b", re.IGNORECASE), "Automação"),
    (re.compile(r"site|loja virtual|e-?commerce", re.IGNORECASE), "Site / loja virtual"),
    (re.compile(r"c[âa]mara|frigor", re.IGNORECASE), "Câmaras frigoríficas"),
    (re.compile(r"limpeza|galp", re.IGNORECASE), "Limpeza industrial"),
)
INTERNAL_CONTENT_MARKERS = (
    "correlation_id",
    "traceback",
    "api_key",
    "api-key",
    "bearer ",
    "openai",
    "system prompt",
    "token:",
    "access_token",
    "refresh_token",
)


def build_conversation_transcript(conversation, *, lead_draft=None) -> str:
    messages = _conversation_messages(conversation)
    if not messages:
        return "Sem histórico registrado nesta conversa."

    selected = messages[-MAX_TRANSCRIPT_TURNS:] if len(messages) > MAX_TRANSCRIPT_TURNS else messages
    omitted = len(messages) - len(selected)
    lines: list[str] = []
    total_chars = 0
    if omitted > 0:
        lines.append(f"... ({omitted} mensagens anteriores omitidas para legibilidade) ...")
        lines.append("")

    for message in selected:
        content = _sanitize_transcript_line(message.content)
        if not content:
            continue
        speaker = "Cliente" if message.role == Message.Role.USER else "Lívia"
        line = f"{speaker}: {content}"
        if total_chars + len(line) > MAX_TRANSCRIPT_CHARS:
            lines.append("... (histórico truncado para caber no e-mail) ...")
            break
        lines.append(line)
        total_chars += len(line)
    return "\n".join(lines) if lines else "Sem histórico registrado nesta conversa."


def build_lead_notification_subject(lead_draft) -> str:
    name = str(getattr(lead_draft, "name", "") or "").strip() or "Lead sem identificação"
    topic = _notification_subject_topic(lead_draft)
    if topic:
        return f"[Lívia] Novo atendimento — {name} — {topic}"
    return f"[Lívia] Novo atendimento — {name}"


def build_lead_notification_summary_text(conversation, *, lead_draft=None) -> str:
    summary = build_conversation_summary(conversation, lead_draft=lead_draft)
    lines: list[str] = []
    if summary.need_summary:
        lines.append(f"Necessidade: {summary.need_summary}")
    if summary.conversation_notes:
        lines.append("Contexto relevante:")
        lines.extend(f"- {note}" for note in summary.conversation_notes[:4])
    if summary.recommended_next_step:
        lines.append(f"Continuidade: {summary.recommended_next_step}")
    return "\n".join(lines).strip() or (summary.need_summary or "Não informado.")


def get_transcript_truncation_notice(conversation) -> str:
    messages = _conversation_messages(conversation)
    notices: list[str] = []
    if len(messages) > MAX_TRANSCRIPT_TURNS:
        notices.append(f"[Histórico limitado aos últimos {MAX_TRANSCRIPT_TURNS} turnos]")
    return " ".join(notices)


def build_lead_notification_body(lead_draft, *, timestamp: str = "") -> str:
    conversation = getattr(lead_draft, "conversation", None)
    summary = build_conversation_summary(conversation, lead_draft=lead_draft)
    transcript = build_conversation_transcript(conversation, lead_draft=lead_draft)
    summary_text = build_lead_notification_summary_text(conversation, lead_draft=lead_draft)
    truncation = get_transcript_truncation_notice(conversation)
    tenant_slug = summary.tenant_slug or str(getattr(getattr(lead_draft, "tenant", None), "slug", "") or "")
    conversation_id = getattr(conversation, "pk", "") or ""
    lead_id = getattr(lead_draft, "pk", "") or ""
    source_page = summary.source_page or str(getattr(conversation, "source_page", "") or "").strip()

    lines = [
        "NOVO ATENDIMENTO — LÍVIA",
        "",
        "DADOS DO CONTATO",
        "",
        f"Nome: {getattr(lead_draft, 'name', '') or 'Não informado'}",
        f"Telefone/WhatsApp: {getattr(lead_draft, 'phone', '') or 'Não informado'}",
        f"E-mail: {getattr(lead_draft, 'email', '') or 'Não informado'}",
    ]
    company = str(getattr(lead_draft, "company", "") or "").strip()
    if company:
        lines.append(f"Empresa: {company}")
    lines.extend(
        [
            "",
            "ASSUNTO",
            "",
            summary.need_summary or str(getattr(lead_draft, "need_summary", "") or "").strip() or "Não informado",
            "",
            "RESUMO DO ATENDIMENTO",
            "",
            summary_text,
            "",
            "HISTÓRICO DA CONVERSA",
            "",
        ]
    )
    if truncation:
        lines.append(truncation)
        lines.append("")
    lines.append(transcript)
    lines.extend(
        [
            "",
            "INFORMAÇÕES INTERNAS",
            "",
            f"Tenant: {tenant_slug or 'não informado'}",
            f"Conversation ID: {conversation_id or 'não informado'}",
            f"Lead ID: {lead_id or 'não informado'}",
            f"Data/hora: {timestamp or 'não informada'}",
        ]
    )
    if source_page:
        lines.append(f"Origem/página: {source_page}")
    return "\n".join(lines)


def _notification_subject_topic(lead_draft) -> str:
    need = str(getattr(lead_draft, "need_summary", "") or "").strip()
    if not need:
        return ""
    normalized = _normalize(need)
    for pattern, label in NOTIFICATION_TOPIC_PATTERNS:
        if pattern.search(normalized):
            return label
    return ""


def build_handoff_notification_body(handoff, *, timestamp: str = "") -> str:
    conversation = getattr(handoff, "conversation", None)
    lead_draft = getattr(handoff, "lead_draft", None)
    summary = build_conversation_summary(conversation, lead_draft=lead_draft)
    transcript = build_conversation_transcript(conversation, lead_draft=lead_draft)
    origin = summary.source_page or str(getattr(handoff, "source_page", "") or "") or "livia-platform"
    reason = str(getattr(handoff, "reason", "") or "").strip() or "solicitação de atendimento"
    tenant_slug = summary.tenant_slug or str(getattr(getattr(handoff, "tenant", None), "slug", "") or "")
    return "\n".join(
        [
            "Solicitação de atendimento da Lívia",
            "",
            f"Tenant: {tenant_slug}",
            f"Data/hora: {timestamp or 'não informada'}",
            f"Origem: {origin}",
            "",
            "Dados disponíveis:",
            f"Nome: {getattr(handoff, 'visitor_name', '') or getattr(lead_draft, 'name', '') or 'Não informado'}",
            f"Empresa: {getattr(handoff, 'visitor_company', '') or getattr(lead_draft, 'company', '') or 'Não informado'}",
            f"Telefone: {getattr(handoff, 'visitor_phone', '') or getattr(lead_draft, 'phone', '') or 'Não informado'}",
            f"E-mail: {getattr(handoff, 'visitor_email', '') or getattr(lead_draft, 'email', '') or 'Não informado'}",
            "",
            f"Motivo do handoff: {reason}",
            "",
            "Resumo da necessidade:",
            summary.need_summary or "Não informada",
            "",
            "Resumo executivo:",
            f"Interesse em {AREA_LABELS.get(summary.service_area, summary.service_area or 'indefinido')}; "
            f"urgência {summary.urgency}; próximo passo: {summary.recommended_next_step}",
            "",
            "Pontos importantes:",
            *[f"- {note}" for note in (summary.conversation_notes or ("Não identificado.",))],
            "",
            "Próxima ação sugerida:",
            "Retornar o contato com o contexto da conversa.",
            "",
            "Histórico da conversa:",
            transcript,
        ]
    )


def _sanitize_transcript_line(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    lowered = cleaned.lower()
    if any(marker in lowered for marker in INTERNAL_CONTENT_MARKERS):
        return ""
    return cleaned[:1200]


def _conversation_messages(conversation):
    if conversation is None:
        return []
    return list(conversation.messages.exclude(role=Message.Role.SYSTEM).order_by("created_at", "id"))


def _substantive_user_messages(messages) -> list[str]:
    values: list[str] = []
    for message in messages:
        if message.role != Message.Role.USER:
            continue
        value = _sanitize(message.content)
        if not value or _is_contact_only(value):
            continue
        values.append(value)
    return values


def _build_corpus(user_messages: list[str], lead_draft) -> str:
    parts = []
    if lead_draft is not None:
        parts.extend(
            str(value or "").strip()
            for value in (lead_draft.need_summary, lead_draft.city)
            if str(value or "").strip()
        )
    parts.extend(user_messages)
    return " ".join(parts)


def _resolve_need_summary(lead_draft, user_messages: list[str]) -> str:
    if lead_draft is not None and str(lead_draft.need_summary or "").strip():
        return str(lead_draft.need_summary).strip()[:500]
    if user_messages:
        return user_messages[0][:500]
    return "Necessidade ainda em detalhamento com a Lívia."


def _resolve_service_area(service_area: str, corpus: str) -> str:
    if service_area and service_area != "unknown":
        return service_area
    normalized = _normalize(corpus)
    if "suporte" in normalized or "login" in normalized:
        return "support"
    return "unknown"


def _extract_products_or_services(corpus: str) -> tuple[str, ...]:
    text = str(corpus or "")
    normalized = _normalize(text)
    found: list[str] = []

    for acronym in re.findall(r"\b[A-Z0-9]{2,}(?:[-/][A-Z0-9]{2,})?\b", text):
        if acronym not in found:
            found.append(acronym)

    patterns = (
        r"\b(?:quero|preciso|para|sobre|de|um|uma|o|a|minha|meu)\s+([a-z0-9çãõáéíóúâêô-]{3,}(?:\s+[a-z0-9çãõáéíóúâêô-]{3,})?)",
        r"\b(?:criar|contratar|comprar|automatizar|arrumar|desenvolver|fazer)\s+([a-z0-9çãõáéíóúâêô-]{3,}(?:\s+[a-z0-9çãõáéíóúâêô-]{3,})?)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, normalized):
            candidate = _clean_candidate(match.group(1))
            if candidate and candidate not in found:
                found.append(candidate)
            if len(found) >= 8:
                return tuple(found)

    for word in re.findall(r"[a-z0-9çãõáéíóúâêô-]{4,}", normalized):
        if word in GENERIC_PRODUCT_STOPWORDS:
            continue
        if not any(word in item.split() for item in found):
            found.append(word)
        if len(found) >= 8:
            break
    return tuple(found[:8])


def _collected_fields(lead_draft) -> tuple[str, ...]:
    if lead_draft is None:
        return tuple()
    labels = (
        ("name", "nome"),
        ("company", "empresa"),
        ("phone", "telefone"),
        ("email", "e-mail"),
        ("city", "cidade"),
    )
    return tuple(label for field_name, label in labels if str(getattr(lead_draft, field_name, "") or "").strip())


def _build_visitor_context(lead_draft) -> str:
    if lead_draft is None:
        return "Sem LeadDraft associado."
    parts = []
    if lead_draft.name:
        parts.append(f"nome {lead_draft.name}")
    if lead_draft.company:
        parts.append(f"empresa {lead_draft.company}")
    if lead_draft.city:
        parts.append(f"cidade {lead_draft.city}")
    return "; ".join(parts) if parts else "Dados do visitante ainda incompletos."


def _conversation_notes(user_messages: list[str]) -> tuple[str, ...]:
    notes = []
    for message in user_messages[-6:]:
        if message not in notes:
            notes.append(message[:220])
    return tuple(notes)


def _detect_urgency(corpus: str, service_area: str) -> str:
    normalized = _normalize(corpus)
    if any(marker in normalized for marker in ("parou", "parada", "urgente", "sem funcionar", "fora do ar")):
        return "alta"
    if service_area == "maintenance" and any(marker in normalized for marker in ("erro", "falha", "problema")):
        return "média"
    return "normal"


def _recommended_next_step(intent: str, service_area: str, urgency: str, corpus: str) -> str:
    normalized = _normalize(corpus)
    if urgency == "alta":
        return "Retornar por telefone/WhatsApp para triagem de urgência e entender o impacto imediato."
    if service_area == "support":
        return "Retornar contato para triagem de suporte com base no histórico."
    if "orcamento" in normalized or "orçamento" in normalized or intent == "quote_request":
        return "Retornar contato para entender escopo e preparar proposta comercial."
    return "Retornar contato para continuar a qualificação com base no histórico."


def _build_title(service_area: str, intent: str) -> str:
    area = AREA_LABELS.get(service_area, service_area or "indefinido")
    if intent == "quote_request":
        return f"Pedido de orçamento - {area}"
    return f"Lead Lívia - {area}"


def _clean_candidate(candidate: str) -> str:
    words = [word for word in str(candidate or "").split() if word not in GENERIC_PRODUCT_STOPWORDS]
    while words and words[0] in {"minha", "meu", "uma", "um", "para"}:
        words.pop(0)
    return " ".join(words[:3]).strip()


def _is_contact_only(text: str) -> bool:
    value = str(text or "").strip()
    normalized = _normalize(value)
    if normalized in {"ok", "sim", "nao", "não", "obrigado", "obrigada"}:
        return True
    if re.fullmatch(r"[+()\d .-]{8,20}", value):
        return True
    if re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", value, flags=re.IGNORECASE):
        return True
    return False


def _sanitize(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if any(marker in cleaned.lower() for marker in ("api_key", "bearer ", "token:", "traceback")):
        return ""
    return cleaned[:1200]


def _normalize(text: str) -> str:
    return str(text or "").strip().lower()
