from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from assistant_core.conversation_turns import (
    build_enrichment_reply,
    is_direct_question,
    is_name_deferred,
    is_need_enrichment,
    merge_need_summaries,
    normalize_text,
)
from assistant_core.state import LeadState, get_current_state

COLLECTION_ACTIVE_KEY = "collection_active"
COLLECTION_PAUSED_KEY = "collection_paused"
CONTACT_DEFERRED_KEY = "contact_collection_deferred"

BUDGET_TRIGGERS = (
    "quero um orcamento",
    "quero uma cotacao",
    "quero uma proposta",
    "gostaria de um orcamento",
    "gostaria de uma cotacao",
    "gostaria de uma proposta",
    "preciso de um orcamento",
    "preciso de uma cotacao",
    "preciso de uma proposta",
    "pode preparar uma proposta",
    "pode registrar",
    "pode encaminhar",
    "pode mandar um orcamento",
    "pode enviar um orcamento",
    "quero saber quanto ficaria",
    "quero orcamento",
    "quero cotacao",
    "quero proposta",
    "orcamento para",
    "cotacao para",
    "proposta para",
)

HIRE_TRIGGERS = (
    "quero contratar",
    "quero fechar",
    "quero comprar",
    "quero seguir com o projeto",
    "quero seguir com",
    "vamos fechar",
    "quero fechar o projeto",
)

HUMAN_TRIGGERS = (
    "quero falar com alguem",
    "quero falar com um especialista",
    "quero falar com especialista",
    "quero falar com vendedor",
    "quero falar com um vendedor",
    "quero atendimento",
    "quero um atendimento",
    "quero atendimento humano",
    "quero contato comercial",
    "falar com especialista",
    "falar com atendente",
    "falar com vendedor",
    "atendimento humano",
    "pode pedir para alguem me ligar",
    "pode me ligar",
    "chama no whatsapp",
    "chama no zap",
    "me passa para um especialista",
    "entrar em contato",
    "entrasse em contato",
    "entre em contato",
    "entrem em contato",
    "alguem entre em contato",
    "alguem entrasse em contato",
    "gostaria que alguem entrasse em contato",
    "gostaria que alguém entrasse em contato",
    "quero que alguem entre em contato",
    "quero que alguém entre em contato",
    "quero contato",
    "preciso de contato",
)

PRICE_QUESTION_MARKERS = (
    "quanto custa",
    "qual o preco",
    "qual o preço",
    "qual valor",
    "quanto fica",
    "quanto sairia",
    "qual o valor",
)

CONSULTATIVE_NEED_MARKERS = (
    "preciso",
    "quero",
    "gostaria",
    "tenho interesse",
    "estou procurando",
    "procuro",
    "busco",
    "tenho problema",
    "tenho um problema",
    "tenho outra duvida",
    "outra duvida",
)


class CollectionTrigger(str, Enum):
    NONE = "none"
    BUDGET = "budget"
    HIRE = "hire"
    HUMAN = "human"


@dataclass(frozen=True)
class CollectionDecision:
    should_collect: bool
    trigger: CollectionTrigger = CollectionTrigger.NONE
    reason: str = ""


def is_conceptual_price_question(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if _has_explicit_actionable_budget(normalized):
        return False
    looks_like_q = "?" in str(text or "") or normalized.startswith(("quanto ", "qual ", "quais "))
    if not looks_like_q:
        return False
    return any(marker in normalized for marker in PRICE_QUESTION_MARKERS)


def _has_explicit_actionable_budget(normalized: str) -> bool:
    if any(term in normalized for term in HUMAN_TRIGGERS):
        return True
    if any(term in normalized for term in HIRE_TRIGGERS):
        return True
    if _looks_like_quote_process_question(normalized):
        return False
    if any(term in normalized for term in BUDGET_TRIGGERS):
        return True
    return bool(
        re.search(r"\b(quero|preciso|gostaria)\s+(?:de\s+)?(?:um\s+)?(orcamento|cotacao|proposta)\b", normalized)
        or re.search(r"\bpode\s+(mandar|enviar)\s+(?:um\s+)?(orcamento|cotacao|proposta)\b", normalized)
    )


def _looks_like_quote_process_question(normalized: str) -> bool:
    """Pergunta sobre processo (medida/foto/planta), não pedido explícito de orçamento."""
    if "orcamento" not in normalized and "cotacao" not in normalized and "proposta" not in normalized:
        return False
    process_markers = (
        "medida", "medicao", "medição", "foto", "planta", "como funciona",
        "mandar", "enviar foto", "vao medir", "vão medir", "preciso de medida",
        "preciso mandar", "posso mandar",
    )
    return any(marker in normalized for marker in process_markers)


def detect_collection_trigger(text: str) -> CollectionTrigger:
    normalized = normalize_text(text)
    if not normalized:
        return CollectionTrigger.NONE
    if is_explicit_human_handoff(text):
        return CollectionTrigger.HUMAN
    looks_like_price_q = (
        ("?" in str(text or "") or normalized.startswith(("quanto ", "qual ", "quais ")))
        and any(marker in normalized for marker in PRICE_QUESTION_MARKERS)
        and not _has_explicit_actionable_budget(normalized)
    )
    if looks_like_price_q:
        return CollectionTrigger.NONE
    if any(term in normalized for term in HUMAN_TRIGGERS):
        return CollectionTrigger.HUMAN
    if any(term in normalized for term in HIRE_TRIGGERS):
        return CollectionTrigger.HIRE
    if any(term in normalized for term in BUDGET_TRIGGERS):
        return CollectionTrigger.BUDGET
    if _looks_like_quote_process_question(normalized):
        return CollectionTrigger.NONE
    if re.search(r"\b(quero|preciso|gostaria)\s+(?:de\s+)?(?:um\s+)?(orcamento|cotacao|proposta)\b", normalized):
        return CollectionTrigger.BUDGET
    if re.search(r"\bpode\s+(mandar|enviar)\s+(?:um\s+)?(orcamento|cotacao|proposta)\b", normalized):
        return CollectionTrigger.BUDGET
    return CollectionTrigger.NONE


def is_explicit_collection_trigger(text: str) -> bool:
    return detect_collection_trigger(text) != CollectionTrigger.NONE


def is_consultative_need_discovery(discovery=None, current_message: str = "") -> bool:
    """Necessidade/interesse inicial não é conversão nem handoff."""
    message = str(current_message or getattr(discovery, "normalized_text", "") or "")
    normalized = normalize_text(message)
    if not normalized:
        return False
    if detect_collection_trigger(normalized) != CollectionTrigger.NONE:
        return False
    if bool(getattr(discovery, "should_collect_lead", False)):
        return False
    if bool(getattr(discovery, "has_contact_data", False)):
        return False
    if bool(getattr(discovery, "has_quote_request", False)) and not is_conceptual_price_question(message):
        return False
    if any(re.search(rf"\b{re.escape(marker)}\b", normalized) for marker in CONSULTATIVE_NEED_MARKERS):
        return True
    return bool(
        getattr(discovery, "has_commercial_interest", False)
        and (
            getattr(discovery, "should_ask_discovery_question", False)
            or getattr(discovery, "should_answer_contextually", False)
        )
    )


def is_human_handoff_request(text: str) -> bool:
    return is_explicit_human_handoff(text)


def is_explicit_human_handoff(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(term in normalized for term in HUMAN_TRIGGERS):
        return True
    from leads.services.handoff import EXPLICIT_HANDOFF_PATTERNS

    for pattern in EXPLICIT_HANDOFF_PATTERNS:
        if " " in pattern or len(pattern) > 10:
            if pattern in normalized:
                return True
        elif re.search(rf"\b{re.escape(pattern)}\b", normalized):
            return True
    if re.search(r"\b(entrasse|entre|entrem)\s+em\s+contato\b", normalized):
        return True
    if re.search(r"\balguem\s+(?:entrasse|entre|entrem)\s+em\s+contato\b", normalized):
        return True
    return False


def _is_direct_need_slot_answer(message: str, lead) -> bool:
    """Resposta curta ao slot need_summary — não reabre coleta para nova descoberta consultiva."""
    from assistant_core.conversation_turns import is_consultative_context_answer, is_direct_question
    from assistant_core.qualification.livia import message_fills_pending_slot

    if not message_fills_pending_slot(message, "need_summary"):
        return False
    if is_direct_question(message):
        return False
    normalized = normalize_text(message)
    exploratory_markers = (
        "preciso",
        "quero",
        "gostaria",
        "queria",
        "tenho interesse",
        "saber sobre",
        "saber mais",
        "na verdade",
        "tambem",
        "também",
    )
    if any(marker in normalized for marker in exploratory_markers):
        return False
    existing = str(getattr(lead, "need_summary", "") or "").strip()
    if not existing:
        return is_consultative_context_answer(message) or len(normalized.split()) <= 6
    return is_consultative_context_answer(message) or len(normalized.split()) <= 6


def collection_already_active(conversation, lead_draft=None) -> bool:
    from leads.services.commercial import resolve_lead_draft

    lead = resolve_lead_draft(conversation, lead_draft)
    if lead is not None:
        data = getattr(lead, "qualification_data", None) or {}
        if isinstance(data, dict):
            if data.get(COLLECTION_PAUSED_KEY) or data.get(CONTACT_DEFERRED_KEY):
                return False
            if data.get(COLLECTION_ACTIVE_KEY):
                return True
    # Soft capture during consultative mode may advance lead_state to
    # COLLECT_NAME_COMPANY without opening explicit name/contact collection.
    # Only treat later commercial states (or the explicit flag above) as active.
    state = get_current_state(conversation) if conversation is not None else LeadState.DISCOVERY
    return state in {LeadState.COLLECT_CONTACT, LeadState.OFFER_HANDOFF}


def pause_collection(lead_draft, *, deferred_contact: bool = True) -> None:
    if lead_draft is None or not hasattr(lead_draft, "qualification_data"):
        return
    data = dict(getattr(lead_draft, "qualification_data", None) or {})
    data[COLLECTION_PAUSED_KEY] = True
    if deferred_contact:
        data[CONTACT_DEFERRED_KEY] = True
    data[COLLECTION_ACTIVE_KEY] = False
    lead_draft.qualification_data = data
    update_fields = ["qualification_data"]
    if hasattr(lead_draft, "updated_at"):
        update_fields.append("updated_at")
    lead_draft.save(update_fields=update_fields)


def mark_collection_active(lead_draft, *, reason: str = "") -> None:
    if lead_draft is None or not hasattr(lead_draft, "qualification_data"):
        return
    data = dict(getattr(lead_draft, "qualification_data", None) or {})
    data[COLLECTION_ACTIVE_KEY] = True
    data[COLLECTION_PAUSED_KEY] = False
    # Novo gatilho explícito reabre coleta mesmo após deferência anterior.
    data[CONTACT_DEFERRED_KEY] = False
    if reason:
        data["collection_trigger_reason"] = str(reason)[:80]
    lead_draft.qualification_data = data
    update_fields = ["qualification_data"]
    if hasattr(lead_draft, "updated_at"):
        update_fields.append("updated_at")
    lead_draft.save(update_fields=update_fields)


def decide_collection(*, current_message: str, conversation=None, lead_draft=None, discovery=None) -> CollectionDecision:
    from assistant_core.dialogue_memory import is_contact_deferred, wants_consultative_continue

    if is_contact_deferred(current_message) or wants_consultative_continue(current_message):
        if lead_draft is not None:
            pause_collection(lead_draft, deferred_contact=True)
        elif conversation is not None:
            try:
                pause_collection(conversation.lead_draft, deferred_contact=True)
            except Exception:
                pass
        return CollectionDecision(False, reason="contact_deferred_consultative")

    trigger = detect_collection_trigger(current_message)
    if collection_already_active(conversation, lead_draft=lead_draft):
        from assistant_core.conversation_turns import is_consultative_context_answer
        from assistant_core.qualification.livia import message_fills_pending_slot
        from leads.services.commercial import QualificationService

        if trigger != CollectionTrigger.NONE:
            return CollectionDecision(True, trigger=trigger, reason="collection_already_active")
        from leads.services.commercial import resolve_lead_draft
        active_lead = resolve_lead_draft(conversation, lead_draft)
        if (active_lead is not None and "need_summary" in QualificationService().missing_fields(active_lead)
                and _is_direct_need_slot_answer(current_message, active_lead)):
            return CollectionDecision(True, trigger=CollectionTrigger.BUDGET, reason="collection_need_enrichment")
        from assistant_core.conversation_turns import is_direct_question, is_need_enrichment
        from assistant_core.services.decision_outcome import (
            is_consultative_knowledge_message,
            is_consultative_knowledge_turn,
        )

        if is_consultative_knowledge_message(current_message):
            return CollectionDecision(False, reason="consultative_knowledge_during_collection")
        from leads.services.commercial import resolve_lead_draft

        active_lead = resolve_lead_draft(conversation, lead_draft)
        if active_lead is not None:
            pending = QualificationService().missing_fields(active_lead)
            if pending:
                if pending[0] == "need_summary" and _is_direct_need_slot_answer(current_message, active_lead):
                    return CollectionDecision(True, trigger=CollectionTrigger.BUDGET, reason="collection_slot_answer")
                if pending[0] != "need_summary" and message_fills_pending_slot(current_message, pending[0]):
                    return CollectionDecision(True, trigger=CollectionTrigger.BUDGET, reason="collection_slot_answer")
        from assistant_core.conversation_turns import is_consultative_context_answer, is_direct_question, is_need_enrichment

        if (
            is_consultative_context_answer(current_message)
            or is_need_enrichment(current_message)
            or is_direct_question(current_message)
        ):
            return CollectionDecision(False, reason="consultative_context_during_collection")
        if is_consultative_knowledge_turn(discovery, current_message):
            return CollectionDecision(False, reason="consultative_knowledge_during_collection")
        return CollectionDecision(True, trigger=CollectionTrigger.BUDGET, reason="collection_already_active")
    if trigger != CollectionTrigger.NONE:
        return CollectionDecision(True, trigger=trigger, reason=f"explicit_{trigger.value}")
    # Visitor volunteered contact data together with commercial/need context.
    if discovery is not None and getattr(discovery, "has_contact_data", False):
        if getattr(discovery, "has_commercial_interest", False) or getattr(discovery, "has_quote_request", False):
            return CollectionDecision(True, trigger=CollectionTrigger.BUDGET, reason="volunteered_contact_with_commercial")
        normalized = normalize_text(current_message)
        if any(
            marker in normalized
            for marker in (
                "preciso", "quero", "gostaria", "orcamento", "orçamento", "automacao", "automação",
                "site", "sistema", "loja", "manutencao", "manutenção", "robo", "robô",
            )
        ):
            return CollectionDecision(True, trigger=CollectionTrigger.BUDGET, reason="volunteered_contact_with_need_markers")
    return CollectionDecision(False, reason="consultative_mode")


def build_conceptual_price_reply(lead_draft=None, *, current_message: str = "") -> str:
    need = str(getattr(lead_draft, "need_summary", "") or "").strip()
    data = dict(getattr(lead_draft, "qualification_data", None) or {}) if lead_draft is not None else {}
    active_domain = str(data.get("active_domain") or "")
    active_entity = str(data.get("active_entity") or "")
    if not active_entity and current_message:
        from assistant_core.dialogue_memory import detect_entity_mention

        detected = detect_entity_mention(current_message)
        if detected:
            active_entity = str(detected.get("canonical") or "")
            active_domain = active_domain or str(detected.get("domain") or "")
    normalized = normalize_text(" ".join([need, active_domain, active_entity, current_message]))
    stone_context = any(
        marker in normalized
        for marker in (
            "cozinha", "bancada", "pia", "cooktop", "banheiro", "lavabo", "escada",
            "gourmet", "granito", "marmore", "mármore", "nicho", "cuba", "churrasqueira", "materials",
        )
    )
    robotics_context = any(
        marker in normalized
        for marker in ("robo", "robô", "robotics", "duno", "dune", "hygibot", "limpeza", "xyron", "mitsubishi", "liro")
    )
    software_context = any(
        marker in normalized
        for marker in ("site", "loja virtual", "software_web", "python", "django", "ecommerce")
    )
    if stone_context and not robotics_context:
        base = (
            "O investimento varia conforme material, medidas, acabamentos e complexidade do projeto. "
            "Ainda não tenho um valor fechado para informar aqui."
        )
    elif robotics_context:
        subject = f" do {active_entity}" if active_entity else ""
        base = (
            f"O investimento{subject} varia conforme o modelo, a aplicação, o ambiente e a complexidade da implantação. "
            "Ainda não tenho um valor fechado confirmado na base atual para informar aqui."
        )
    elif software_context:
        base = (
            "O investimento de um site ou sistema varia conforme escopo, integrações e conteúdo. "
            "Ainda não tenho um valor fechado para informar aqui."
        )
    else:
        base = (
            "O investimento varia conforme o escopo do projeto e os requisitos envolvidos. "
            "Ainda não tenho um valor fechado para informar aqui."
        )
    if need or active_entity:
        return f"{base} Se quiser, posso levantar os pontos do seu projeto para preparar um orçamento."
    return f"{base} Se quiser um orçamento, me diga e eu sigo com os dados necessários."


def build_consultative_commercial_reply(*, lead_draft=None, current_message: str = "", history=None) -> str:
    need = str(getattr(lead_draft, "need_summary", "") or "").strip()
    message = str(current_message or "").strip()
    if is_conceptual_price_question(message):
        return build_conceptual_price_reply(lead_draft, current_message=message)
    if is_direct_question(message):
        from assistant_core.conversation_turns import build_direct_question_reply, detect_question_type

        return build_direct_question_reply(
            lead_draft,
            question_type=detect_question_type(message),
            current_message=message,
        )
    normalized = normalize_text(message)
    if any(token in normalized for token in ("loja virtual", "ecommerce", "e-commerce", "loja online")):
        return (
            "Entendi, uma loja virtual. Para eu te orientar melhor: "
            "você pretende vender poucos produtos no início ou já tem um catálogo maior?"
        )
    if any(token in normalized for token in ("site", "pagina", "página", "portal", "sistema web", "sistema")):
        return (
            "Claro. Posso te ajudar com isso. "
            "Qual é o objetivo principal: divulgação, captura de contatos, vendas online ou um sistema interno?"
        )
    if is_need_enrichment(message):
        return build_enrichment_reply(lead_draft, current_message=message)
    if any(token in normalized for token in ("escola", "educac", "professor", "bncc")):
        return (
            "Entendi o contexto educacional. Para orientar melhor: "
            "o objetivo é robótica educacional, demonstração tecnológica ou outro uso na escola?"
        )
    if any(token in normalized for token in ("automacao", "automação", "robo", "robô", "robotica", "robótica")):
        return (
            "Entendi. Para te orientar melhor: qual ambiente você quer atender "
            "e qual objetivo principal (recepção, segurança, limpeza, educação ou outro)?"
        )
    # Marmoraria / pedras naturais (Pitondo e verticais similares)
    if any(token in normalized for token in ("cozinha", "bancada", "pia", "cooktop", "ilha", "frontao", "frontão")):
        return (
            "Entendi. Para te orientar melhor: o projeto é de cozinha — "
            "bancada, pia, ilha ou outro detalhe específico?"
        )
    if any(token in normalized for token in ("banheiro", "lavabo", "cuba", "nicho")):
        return (
            "Entendi o banheiro/lavabo. Você precisa de bancada, cuba, nicho "
            "ou mais de um desses itens?"
        )
    if any(token in normalized for token in ("escada", "escadas")):
        return (
            "Entendi. Para escadas, me conta se é escada completa, revestimento "
            "ou outro detalhe do projeto."
        )
    if any(token in normalized for token in ("gourmet", "churrasqueira")):
        return (
            "Entendi a área gourmet. A bancada é para apoio, pia, churrasqueira "
            "ou outro uso?"
        )
    if any(token in normalized for token in ("granito", "marmore", "mármore", "quartzito", "pedra")):
        return (
            "Entendi. Qual ambiente você quer atender com a pedra "
            "(cozinha, banheiro, escada, área gourmet ou outro)?"
        )
    if need:
        return (
            f"Entendi. Com o contexto de {_short(need)}, posso te orientar melhor. "
            "Qual detalhe é mais importante agora: material, medidas ou acabamento?"
        )
    return "Claro. Pode me contar um pouco mais sobre o que você precisa fazer ou resolver?"


def soft_merge_need_from_history(*, lead_draft, history, message: str) -> str:
    existing = str(getattr(lead_draft, "need_summary", "") or "").strip() if lead_draft is not None else ""
    merged = existing
    for item in history or []:
        if str(item.get("role") or "") != "user":
            continue
        content = str(item.get("content") or "").strip()
        if not content or is_name_deferred(content) or is_conceptual_price_question(content) or is_direct_question(content):
            continue
        if detect_collection_trigger(content) == CollectionTrigger.NONE:
            merged = merge_need_summaries(merged, content)
    if (
        message
        and not is_name_deferred(message)
        and not is_direct_question(message)
        and detect_collection_trigger(message) == CollectionTrigger.NONE
    ):
        merged = merge_need_summaries(merged, message)
    return merged[:500]


def _short(text: str) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    if len(cleaned) <= 90:
        return cleaned.rstrip(".")
    return cleaned[:87].rstrip() + "…"
