from __future__ import annotations

from dataclasses import dataclass


class LeadState:
    DISCOVERY = "discovery"
    OFFER_HANDOFF = "offer_handoff"
    COLLECT_NEED = "collect_need"
    COLLECT_NAME_COMPANY = "collect_name_company"
    COLLECT_CONTACT = "collect_contact"
    QUALIFIED = "qualified"
    CLOSED = "closed"


STATE_ORDER = (
    LeadState.DISCOVERY,
    LeadState.OFFER_HANDOFF,
    LeadState.COLLECT_NEED,
    LeadState.COLLECT_NAME_COMPANY,
    LeadState.COLLECT_CONTACT,
    LeadState.QUALIFIED,
    LeadState.CLOSED,
)

NEW_CYCLE_MARKERS = (
    "novo orçamento",
    "novo orcamento",
    "outra solicitação",
    "outra solicitacao",
    "novo pedido",
    "outro projeto",
    "nova demanda",
)


@dataclass(frozen=True)
class LeadStateSnapshot:
    state: str
    next_field: str = ""
    is_terminal: bool = False


def normalize_state(raw_state: str) -> str:
    if raw_state in STATE_ORDER:
        return raw_state
    return LeadState.DISCOVERY


def get_current_state(conversation) -> str:
    return normalize_state(getattr(conversation, "lead_state", LeadState.DISCOVERY))


def set_state(conversation, state: str) -> str:
    next_state = normalize_state(state)
    if getattr(conversation, "lead_state", None) == next_state:
        return next_state
    conversation.lead_state = next_state
    conversation.save(update_fields=["lead_state", "updated_at"])
    return next_state


def should_lock_lead(conversation) -> bool:
    return bool(getattr(conversation, "is_qualified", False)) or get_current_state(conversation) in {
        LeadState.QUALIFIED,
        LeadState.CLOSED,
    }


def should_block_dialogue_for_locked_lead(conversation, message: str, discovery=None) -> bool:
    """Bloqueia só tentativas comerciais duplicadas — não dúvidas consultivas."""
    if not should_lock_lead(conversation):
        return False
    if can_start_new_cycle(conversation, message):
        return False
    if discovery is None:
        from assistant_core.discovery import analyze_message

        discovery = analyze_message(message)
    from assistant_core.consultative_policy import is_consultative_need_discovery
    from assistant_core.services.decision_outcome import is_consultative_knowledge_turn

    if is_consultative_knowledge_turn(discovery, message) or is_consultative_need_discovery(discovery, message):
        return False
    from assistant_core.consultative_policy import CollectionTrigger, detect_collection_trigger

    if detect_collection_trigger(message) != CollectionTrigger.NONE:
        return True
    if bool(getattr(discovery, "should_collect_lead", False)):
        return True
    if getattr(discovery, "intent", "") == "contact_data" and bool(getattr(discovery, "has_contact_data", False)):
        return True
    return False


def can_start_new_cycle(conversation, message: str) -> bool:
    if not should_lock_lead(conversation):
        return True
    normalized = str(message or "").strip().lower()
    if any(marker in normalized for marker in NEW_CYCLE_MARKERS):
        return True
    from assistant_core.consultative_policy import is_consultative_need_discovery
    from assistant_core.discovery import analyze_message

    return is_consultative_need_discovery(analyze_message(message), message)


def _commercial_capture_active(conversation, lead_draft, intent: str = "") -> bool:
    from assistant_core.consultative_policy import COLLECTION_ACTIVE_KEY, collection_already_active

    data = getattr(lead_draft, "qualification_data", None) or {}
    if isinstance(data, dict) and data.get(COLLECTION_ACTIVE_KEY):
        return True
    if collection_already_active(conversation, lead_draft):
        return True
    if intent == "contact_data":
        return True
    return False


def next_state_after_message(conversation, lead_draft, intent: str = "", extracted_data=None) -> LeadStateSnapshot:
    if lead_draft is None:
        return LeadStateSnapshot(state=get_current_state(conversation))
    if getattr(lead_draft, "status", "") in {"sent_to_crm", "qualified"} or getattr(conversation, "is_qualified", False):
        return LeadStateSnapshot(state=LeadState.QUALIFIED, is_terminal=True)
    if not _commercial_capture_active(conversation, lead_draft, intent):
        return LeadStateSnapshot(state=LeadState.DISCOVERY)
    from leads.services.commercial import QualificationService
    missing = QualificationService().missing_fields(lead_draft)
    if missing:
        field = missing[0]
        state = (LeadState.COLLECT_NEED if field == "need_summary" else
                 LeadState.COLLECT_NAME_COMPANY if field in {"name", "company"} else LeadState.COLLECT_CONTACT)
        return LeadStateSnapshot(state=state, next_field=field)
    if intent in {"quote_request", "commercial_interest", "contact_data"}:
        return LeadStateSnapshot(state=LeadState.OFFER_HANDOFF)
    return LeadStateSnapshot(state=LeadState.DISCOVERY)
