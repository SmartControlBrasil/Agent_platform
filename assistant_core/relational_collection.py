"""Coleta relacional: nome e contato no momento certo, sem formulário."""

from __future__ import annotations

import re

from assistant_core.conversation_turns import is_direct_question, normalize_text
from assistant_core.consultative_policy import COLLECTION_ACTIVE_KEY, collection_already_active
from assistant_core.qualification import (
    infer_pending_field_values,
    is_valid_company,
    is_valid_email,
    is_valid_name,
    is_valid_need_summary,
    is_valid_phone,
)
from assistant_core.services.deterministic_synthesis import is_generic_fallback_reply
from leads.services.commercial import FIELD_SOURCE_EXPLICIT, merge_field_value, normalize_email, normalize_name, normalize_phone, resolve_lead_draft

RELATIONAL_NAME_ASKED_KEY = "relational_name_asked"
RELATIONAL_NAME_CAPTURED_KEY = "relational_name_captured"
CONTACT_REASON_SHOWN_KEY = "contact_reason_shown"

B2B_CONTEXT_MARKERS = (
    "minha empresa",
    "nossa empresa",
    "minha fabrica",
    "minha fábrica",
    "nosso galpao",
    "nosso galpão",
    "nosso condominio",
    "nosso condomínio",
    "nossa escola",
    "nosso hospital",
    "nosso hotel",
    "nossa industria",
    "nossa indústria",
    "meu comercio",
    "meu comércio",
    "nossa loja",
    "nossa fabrica",
    "nossa fábrica",
    "para a empresa",
    "para minha empresa",
    "para nossa empresa",
    "empresa tem",
    "empresa possui",
    "câmaras",
    "camaras",
    "frigoríf",
    "frigorif",
)

URGENT_DIAGNOSTIC_MARKERS = (
    "fumaca",
    "fumaça",
    "faisca",
    "faísca",
    "cheiro de queimado",
    "explodiu",
    "vazamento",
    "risco",
    "urgente",
    "pegando fogo",
    "sem funcionar",
    "desligando sozinho",
    "solta fumaça",
    "soltando fumaça",
)

ISOLATED_INFO_MARKERS = (
    "qual o telefone",
    "qual o telefone de voces",
    "qual o telefone de vocês",
    "telefone de voces",
    "telefone de vocês",
    "o que e automacao",
    "o que é automação",
    "o que e automacao industrial",
    "o que é automação industrial",
    "onde fica",
    "horario de atendimento",
    "horário de atendimento",
)


def build_relational_name_prompt() -> str:
    return "Aliás, como posso te chamar?"


def build_collection_name_prompt() -> str:
    return "Certo. Como posso te chamar?"


def has_valid_name(lead) -> bool:
    return bool(lead is not None and is_valid_name(getattr(lead, "name", "")))


def has_b2b_context(*, lead=None, history=None, message: str = "") -> bool:
    parts = [str(message or "")]
    if lead is not None:
        if is_valid_company(getattr(lead, "company", "")):
            return True
        parts.append(str(getattr(lead, "need_summary", "") or ""))
    for item in list(history or [])[-8:]:
        if item.get("role") == "user":
            parts.append(str(item.get("content") or ""))
    blob = normalize_text(" ".join(parts))
    return any(marker in blob for marker in B2B_CONTEXT_MARKERS)


def is_urgent_diagnostic(message: str) -> bool:
    normalized = normalize_text(message)
    return any(marker in normalized for marker in URGENT_DIAGNOSTIC_MARKERS)


def is_isolated_informational(message: str, history=None) -> bool:
    normalized = normalize_text(message).strip(" .!?;")
    if any(marker in normalized for marker in ISOLATED_INFO_MARKERS):
        return True
    user_turns = [item for item in (history or []) if item.get("role") == "user"]
    if len(user_turns) <= 1 and is_direct_question(message):
        if normalized.startswith(("o que ", "qual ", "como funciona", "onde ", "quando ")):
            return True
    return False


def _consultative_exchange_count(history) -> int:
    messages = list(history or [])
    pairs = 0
    for index, item in enumerate(messages):
        if item.get("role") != "user":
            continue
        if index + 1 < len(messages) and messages[index + 1].get("role") == "assistant":
            pairs += 1
    return pairs


def _has_ongoing_topic(*, lead, history, message: str) -> bool:
    if lead is not None and is_valid_need_summary(getattr(lead, "need_summary", "")):
        return True
    data = getattr(lead, "qualification_data", None) or {}
    if data.get("continuity_offer_need") or data.get("active_need"):
        return True
    blob = normalize_text(
        " ".join(
            str(item.get("content") or "")
            for item in list(history or [])[-6:]
            if item.get("role") == "user"
        )
        + " "
        + str(message or "")
    )
    topic_markers = (
        "galpao", "galpão", "robo", "robô", "duno", "limpeza", "automacao", "automação",
        "camara", "câmara", "frigorif", "ar-condicionado", "compressor", "equipamento",
        "projeto", "orcamento", "orçamento", "escola", "empresa", "fabrica", "fábrica",
    )
    return any(marker in blob for marker in topic_markers)


def should_offer_relational_name(
    *,
    conversation,
    lead,
    history,
    message: str,
    reply: str,
    knowledge_context: str = "",
    handoff: bool = False,
) -> bool:
    if conversation is None or handoff or collection_already_active(conversation, lead):
        return False
    lead = resolve_lead_draft(conversation, lead)
    if lead is None:
        return False
    data = dict(lead.qualification_data or {})
    if has_valid_name(lead) or data.get(RELATIONAL_NAME_ASKED_KEY) or data.get(RELATIONAL_NAME_CAPTURED_KEY):
        return False
    if data.get("continuity_offer_status") == "pending":
        return False
    if is_urgent_diagnostic(message) or is_isolated_informational(message, history):
        return False
    if is_generic_fallback_reply(reply) or len(str(reply or "").strip()) < 12:
        return False
    if _consultative_exchange_count(history) < 2:
        return False
    if not _has_ongoing_topic(lead=lead, history=history, message=message):
        return False
    if build_relational_name_prompt().lower() in str(reply or "").lower():
        return False
    return True


def mark_relational_name_asked(lead) -> None:
    if lead is None:
        return
    data = dict(getattr(lead, "qualification_data", None) or {})
    data[RELATIONAL_NAME_ASKED_KEY] = True
    lead.qualification_data = data
    lead.save(update_fields=["qualification_data", "updated_at"])


def capture_passive_fields(*, lead, message: str, history=None) -> bool:
    """Captura nome/contato passivamente sem ativar coleta comercial."""
    if lead is None:
        return False
    data = dict(getattr(lead, "qualification_data", None) or {})
    if data.get(COLLECTION_ACTIVE_KEY):
        return False
    changed = False
    if data.get(RELATIONAL_NAME_ASKED_KEY) and not has_valid_name(lead):
        inferred = infer_pending_field_values(message, "name")
        name = inferred.get("name")
        if name and is_valid_name(normalize_name(name)):
            changed |= merge_field_value(lead, "name", normalize_name(name), source=FIELD_SOURCE_EXPLICIT)
            data[RELATIONAL_NAME_CAPTURED_KEY] = True
            data[RELATIONAL_NAME_ASKED_KEY] = False
    if not has_valid_name(lead):
        inferred = infer_pending_field_values(message, "name")
        name = inferred.get("name")
        if name and is_valid_name(normalize_name(name)) and any(
            marker in normalize_text(message)
            for marker in ("meu nome", "me chamo", "sou o", "sou a")
        ):
            changed |= merge_field_value(lead, "name", normalize_name(name), source=FIELD_SOURCE_EXPLICIT)
            data[RELATIONAL_NAME_CAPTURED_KEY] = True
    for field_name, validator, normalizer in (
        ("phone", is_valid_phone, normalize_phone),
        ("email", is_valid_email, normalize_email),
        ("company", is_valid_company, normalize_name),
    ):
        current = str(getattr(lead, field_name, "") or "").strip()
        if current and validator(current):
            continue
        inferred = infer_pending_field_values(message, field_name)
        value = inferred.get(field_name)
        if value and validator(normalizer(value)):
            changed |= merge_field_value(lead, field_name, normalizer(value), source=FIELD_SOURCE_EXPLICIT)
    if changed:
        lead.qualification_data = data
        lead.save(update_fields=["name", "phone", "email", "company", "field_sources", "qualification_data", "updated_at"])
    elif data != dict(getattr(lead, "qualification_data", None) or {}):
        lead.qualification_data = data
        lead.save(update_fields=["qualification_data", "updated_at"])
    return changed


def pending_collection_fields(*, lead, history=None, message: str = "") -> list[str]:
    """Campos a perguntar agora, somente com coleta comercial ativa."""
    if lead is None or not (lead.qualification_data or {}).get(COLLECTION_ACTIVE_KEY):
        return []
    pending: list[str] = []
    if not has_valid_name(lead):
        pending.append("name")
    if not is_valid_phone(getattr(lead, "phone", "")):
        pending.append("phone")
    if not is_valid_email(getattr(lead, "email", "")):
        pending.append("email")
    if (
        has_b2b_context(lead=lead, history=history, message=message)
        and not is_valid_company(getattr(lead, "company", ""))
    ):
        pending.append("company")
    return pending


def build_contact_collection_prompt(
    *,
    lead,
    field: str,
    invalid_fields: list[str] | None = None,
) -> str:
    invalid_fields = list(invalid_fields or [])
    data = dict(getattr(lead, "qualification_data", None) or {})
    if field == "name":
        if "name" in invalid_fields:
            return "Pode me informar seu nome real?"
        return build_collection_name_prompt()
    if field == "phone":
        prefix = ""
        if not data.get(CONTACT_REASON_SHOWN_KEY):
            prefix = (
                "Para nossa equipe retornar seu atendimento, preciso de um telefone ou WhatsApp e de um e-mail. "
            )
        if "phone" in invalid_fields:
            return f"{prefix}Esse telefone ficou incompleto. Qual é o melhor telefone ou WhatsApp?"
        return f"{prefix}Qual é o melhor telefone ou WhatsApp?"
    if field == "email":
        if "email" in invalid_fields:
            return "Esse e-mail parece incompleto. Qual é o seu e-mail?"
        return "Qual é o seu e-mail?"
    if field == "company":
        return "Qual é o nome da empresa ou instituição?"
    return ""

def mark_contact_reason_shown(lead) -> None:
    if lead is None:
        return
    data = dict(getattr(lead, "qualification_data", None) or {})
    data[CONTACT_REASON_SHOWN_KEY] = True
    data["privacy_notice_shown"] = True
    lead.qualification_data = data
    lead.save(update_fields=["qualification_data", "updated_at"])
