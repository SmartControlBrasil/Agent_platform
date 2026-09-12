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


def _need_for_offer(message, history):
    if concrete_need(message):
        return message
    text = normalize_text(message)
    if is_direct_question(message) or not re.search(r"\b(\d+|dois|duas|tres|quatro|cinco)\b", text):
        return ""
    for item in reversed(list(history or [])[-6:]):
        previous = item.get("content", "")
        if item.get("role") != "user" or is_explicit_collection_trigger(previous):
            continue
        if not re.match(r"(?:preciso|busco|procuro|necessito|meu|minha|nosso|nossa)\b", normalize_text(previous)):
            continue
        candidate = f"{previous.rstrip('.')}. {message}"
        if concrete_need(candidate):
            return candidate
    return ""


def offer_after_guidance(*, conversation, message, history, reply, knowledge_context, handoff=False):
    if conversation is None or handoff or conversation.is_qualified:
        return reply, ""
    lead = resolve_lead_draft(conversation)
    data = dict(getattr(lead, "qualification_data", None) or {})
    if (collection_already_active(conversation, lead) or is_explicit_collection_trigger(message)
            or data.get("contact_collection_deferred") or data.get("collection_paused")
            or conversation.lead_state in {"qualified", "closed"}):
        return reply, ""
    need = _need_for_offer(message, history)
    if need and is_valid_need_summary(need) and knowledge_context.strip():
        # A concrete need already answers the generic discovery prompt.
        reply = reply.removesuffix("Claro. Pode me contar um pouco mais sobre o que você precisa fazer ou resolver?").rstrip()
    # Diagnostic questions and insufficient evidence must be resolved before offering.
    if (not knowledge_context.strip() or "?" in reply or is_generic_fallback_reply(reply)
            or len(reply.split()) < 8 or re.search(r"nao (?:encontrei|tenho|ha).*?(?:informacao|evidencia)", normalize_text(reply))):
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
    if not is_valid_need_summary(need):
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
