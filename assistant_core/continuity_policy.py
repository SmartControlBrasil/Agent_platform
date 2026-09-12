"""Deterministic opt-in for follow-up after grounded guidance."""

import re

from assistant_core.consultative_policy import (
    collection_already_active, is_explicit_collection_trigger,
)
from assistant_core.conversation_turns import normalize_text, is_direct_question
from assistant_core.qualification import is_valid_need_summary
from assistant_core.services.deterministic_synthesis import is_generic_fallback_reply
from leads.services.commercial import (
    FIELD_SOURCE_EXPLICIT, merge_field_value, resolve_lead_draft,
)

OFFER = "Se quiser, posso registrar o caso para nossa equipe acompanhar."
DECLINE_REPLY = "Tudo bem. Se precisar de mais alguma orientação, posso ajudar."
STATUS_KEY = "continuity_offer_status"

OPERATIONAL_COLLECTION_REPLY_PHRASES = frozenset(
    {
        "pode registrar",
        "pode encaminhar",
        "pode sim",
        "pode",
        "sim",
        "ok",
        "claro",
        "vamos",
    }
)

GENERIC_FOLLOWUP_PATTERNS = (
    r"\bquer(?:\s+que\s+eu)?\s+(?:que\s+eu\s+)?(?:detalhe|explic(?:o|ar)|saber\s+mais|ver\s+mais|aprofundar)",
    r"\bposso\s+(?:te\s+)?(?:ajudar\s+a\s+)?(?:detalhar|explicar|esclarecer|montar|aprofundar|ajudar)",
    r"\bcomo\s+posso\s+(?:te\s+)?ajudar(?:\s+mais)?",
    r"\btem\s+mais\s+alguma\s+d[úu]vida",
    r"\balguma\s+d[úu]vida",
    r"\bquer\s+que\s+eu\s+monte",
    r"\bqual\b.*\b(?:voce|você)\b.*\b(?:precisa|quer|busca|procura|objetivo)\b",
)


def is_operational_collection_reply(message: str) -> bool:
    return normalize_text(message).strip(" .!?;") in OPERATIONAL_COLLECTION_REPLY_PHRASES


def resolve_offer_response(*, conversation, message, history):
    lead = resolve_lead_draft(conversation)
    if lead is None or (lead.qualification_data or {}).get(STATUS_KEY) != "pending":
        return ""
    if is_explicit_collection_trigger(message):
        status = "accepted"
    else:
        text = normalize_text(message).strip(" .!?;")
        if text in {"nao", "nao precisa", "agora nao", "so queria saber isso", "obrigado", "obrigada", "deixa para depois"}:
            status = "declined"
        elif text in {"pode registrar", "pode encaminhar"}:
            status = "accepted"
        elif text in {"sim", "pode", "pode sim", "ok", "claro", "quero", "vamos"}:
            last_reply = next((item.get("content", "") for item in reversed(list(history or [])) if item.get("role") == "assistant"), "")
            # A short yes after a diagnostic question answers that question, not the offer.
            if not last_reply or ("?" in last_reply and OFFER not in last_reply):
                return ""
            status = "accepted"
        else:
            return ""
    data = dict(lead.qualification_data or {})
    data[STATUS_KEY] = status
    lead.qualification_data = data
    lead.save(update_fields=["qualification_data", "updated_at"])
    return status


def concrete_need(text):
    text = normalize_text(text)
    if re.search(r"\b(pesquisa|academico|academica|curiosidade|estudando|trabalho escolar)\b", text):
        return False
    if text.startswith(("o que", "qual a diferenca", "como funciona", "voces trabalham", "voces tem")):
        return False
    if re.search(r"\b(entender|aprender|conceito|significa|comparacao|diferenca)\b", text):
        return False
    if re.search(r"\bnao (?:preciso|quero|necessito|busco)\b", text):
        return False
    project = bool(
        re.search(r"\b(preciso|busco|procuro|necessito)\b", text)
        and re.search(r"\b(automatizar|instalar|instalacao|manutencao|projeto|dimensionar|robo|sistema|solucao)\b", text)
        and len(text.split()) >= 8
    )
    fault = bool(
        re.search(r"\b(meu|minha|nosso|nossa)\b", text)
        and re.search(r"\b(erro|falha|desliga|desligando|queimado|fumaca|faisca|para de funcionar)\b", text)
        and len(text.split()) >= 9
    )
    return project or fault


def is_generic_followup_question(question: str) -> bool:
    """Pergunta opcional de conversa, sem valor diagnóstico para o projeto."""
    normalized = normalize_text(question)
    if "?" not in str(question or ""):
        return False
    return any(re.search(pattern, normalized) for pattern in GENERIC_FOLLOWUP_PATTERNS)


def has_diagnostic_question(reply: str) -> bool:
    """True quando há pergunta que pede dado técnico/operacional real."""
    questions = _question_segments(reply)
    if not questions:
        return False
    return any(not is_generic_followup_question(question) for question in questions)


def strip_generic_followup_questions(reply: str) -> str:
    """Remove perguntas genéricas para que a oferta determinística possa ocupar o turno."""
    text = str(reply or "").strip()
    if not text or "?" not in text:
        return text
    kept: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+|\n+", text):
        part = part.strip()
        if not part:
            continue
        if "?" in part and is_generic_followup_question(part):
            continue
        kept.append(part)
    cleaned = " ".join(kept).strip()
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _question_segments(reply: str) -> list[str]:
    text = str(reply or "").strip()
    if "?" not in text:
        return []
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", text)
        if "?" in part and part.strip()
    ]


def _need_for_offer(message, history, lead=None):
    existing_need = str(getattr(lead, "need_summary", "") or "").strip()
    if is_valid_need_summary(existing_need):
        if _needs_more_turns(existing_need, history, message):
            return ""
        return existing_need
    if concrete_need(message):
        return message
    for item in reversed(list(history or [])[-6:]):
        previous = item.get("content", "")
        if item.get("role") != "user" or is_explicit_collection_trigger(previous):
            continue
        if not re.match(r"(?:preciso|busco|procuro|necessito|meu|minha|nosso|nossa)\b", normalize_text(previous)):
            continue
        candidate = f"{previous.rstrip('.')}. {message}"
        if concrete_need(candidate):
            return candidate
    accumulated = _accumulated_need(message, history)
    if accumulated:
        return accumulated
    return ""


def _accumulated_need(message: str, history) -> str:
    user_parts = [
        str(item.get("content") or "").strip()
        for item in list(history or [])[-6:]
        if item.get("role") == "user" and str(item.get("content") or "").strip()
    ]
    user_parts.append(str(message or "").strip())
    part_count = len([part for part in user_parts if part])
    if part_count < 2:
        return ""
    candidate = " ".join(part for part in user_parts if part).strip()
    normalized = normalize_text(candidate)
    current = normalize_text(message)
    if not candidate or not _is_offerable_need(candidate):
        return ""
    if _needs_more_turns(candidate, history, message):
        return ""
    if re.search(r"\b(pesquisa|academico|academica|curiosidade|trabalho escolar|conceito|significa|comparacao|diferenca)\b", normalized):
        return ""
    if is_direct_question(message) and not _message_adds_requirement(current):
        return ""
    if not (_has_project_intent(normalized) and _has_concrete_requirement(normalized) and _message_adds_requirement(current)):
        return ""
    return candidate[:500]


def _is_offerable_need(text: str) -> bool:
    if is_valid_need_summary(text):
        return True
    normalized = normalize_text(text)
    if re.search(r"\b(pesquisa|academico|academica|curiosidade|trabalho escolar|conceito|significa|comparacao|diferenca)\b", normalized):
        return False
    content_words = set(re.findall(r"\b[a-z0-9]{4,}\b", normalized))
    content_words -= {"preciso", "quero", "gostaria", "tenho", "temos", "para", "com", "sem", "mais", "menos"}
    return len(content_words) >= 4


def _needs_more_turns(need: str, history, message: str) -> bool:
    normalized = normalize_text(need)
    if not re.search(r"\b(estudando|pesquisando|avaliando|entendendo)\b", normalized):
        return False
    user_turns = [
        item for item in list(history or []) if item.get("role") == "user" and str(item.get("content") or "").strip()
    ]
    if str(message or "").strip():
        user_turns.append({"role": "user", "content": message})
    return len(user_turns) < 3


def _has_project_intent(normalized: str) -> bool:
    return bool(
        re.search(r"\b(preciso|quero|gostaria|busco|procuro|tenho|temos|melhorar|aumentar|reduzir|modernizar|padronizar)\b", normalized)
    )


def _has_concrete_requirement(normalized: str) -> bool:
    content_words = re.findall(r"\b[a-z0-9]{4,}\b", normalized)
    if len(set(content_words)) < 6:
        return False
    if len(set(content_words)) >= 7:
        return True
    return bool(
        re.search(r"\b(para|com|sem|em|no|na|nos|nas|por|mais|menos|melhor|cada|todo|toda|todos|todas)\b", normalized)
        or re.search(r"\b\d+(?:[.,]\d+)?\b", normalized)
    )


def _message_adds_requirement(normalized: str) -> bool:
    return bool(
        re.search(r"\b(preciso|quero|gostaria|busco|procuro|tenho|temos|deve|deveria|pode|sem|com|para|mais|menos|melhor|aumentar|reduzir|modernizar|padronizar|gerar|acompanhar|visualizar|receber)\b", normalized)
        or re.search(r"\b\d+(?:[.,]\d+)?\b", normalized)
    )


def offer_after_guidance(*, conversation, message, history, reply, knowledge_context, handoff=False):
    if conversation is None or handoff or conversation.is_qualified:
        return reply, ""
    lead = resolve_lead_draft(conversation)
    data = dict(getattr(lead, "qualification_data", None) or {})
    if (collection_already_active(conversation, lead) or is_explicit_collection_trigger(message)
            or data.get("contact_collection_deferred") or data.get("collection_paused")
            or conversation.lead_state in {"qualified", "closed"}):
        return reply, ""
    need = _need_for_offer(message, history, lead=lead)
    if need and _is_offerable_need(need) and knowledge_context.strip():
        # A concrete need already answers the generic discovery prompt.
        reply = reply.removesuffix("Claro. Pode me contar um pouco mais sobre o que você precisa fazer ou resolver?").rstrip()
    # Diagnostic questions and insufficient evidence must be resolved before offering.
    if not knowledge_context.strip() or is_generic_fallback_reply(reply) or len(reply.split()) < 8:
        return reply, ""
    if re.search(r"nao (?:encontrei|tenho|ha).*?(?:informacao|evidencia)", normalize_text(reply)):
        return reply, ""
    if "?" in reply:
        if has_diagnostic_question(reply):
            return reply, ""
        if need and _is_offerable_need(need):
            reply = strip_generic_followup_questions(reply)
        else:
            return reply, ""
    if not need:
        return reply, ""
    if data.get(STATUS_KEY):
        # Reoffer only for an explicitly new concrete need, never a routine follow-up.
        if not re.search(r"\b(nova necessidade|outro problema|outro projeto)\b", normalize_text(message)):
            return reply, ""
    # Require application-specific overlap with the tenant-scoped retrieved evidence.
    words = set(re.findall(r"\b[a-z]{5,}\b", normalize_text(need)))
    words -= {"preciso", "nosso", "nossa", "problema", "quero", "minha", "depois", "outro", "tenho", "ajuda"}
    if len(words & set(re.findall(r"\b[a-z]{5,}\b", normalize_text(knowledge_context)))) < 2:
        return reply, ""
    if not _is_offerable_need(need):
        return reply, ""
    from leads.services.lead_capture import LeadCaptureService
    lead = lead or LeadCaptureService().get_or_create_lead_draft(conversation)
    # Store the validated need without qualifying or dispatching a consultative draft.
    if not is_valid_need_summary(lead.need_summary):
        merge_field_value(lead, "need_summary", need[:500], source=FIELD_SOURCE_EXPLICIT)
    data = dict(lead.qualification_data or {})
    data[STATUS_KEY] = "pending"
    data["continuity_offer_need"] = need[:500]
    lead.qualification_data = data
    lead.save(update_fields=["need_summary", "field_sources", "qualification_data", "updated_at"])
    return f"{reply.rstrip()}\n\n{OFFER}", "offered"
