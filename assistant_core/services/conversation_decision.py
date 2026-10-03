from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ConversationAction(StrEnum):
    ANSWER = "ANSWER"
    ASK_NAME = "ASK_NAME"
    ASK_PHONE = "ASK_PHONE"
    ASK_EMAIL = "ASK_EMAIL"
    ASK_COMPANY = "ASK_COMPANY"
    ANSWER_AND_RESUME_COLLECTION = "ANSWER_AND_RESUME_COLLECTION"
    COMPLETE_HANDOFF = "COMPLETE_HANDOFF"
    FALLBACK = "FALLBACK"


FIELD_ACTIONS = {
    "name": ConversationAction.ASK_NAME,
    "name_or_company": ConversationAction.ASK_NAME,
    "phone": ConversationAction.ASK_PHONE,
    "phone_or_email": ConversationAction.ASK_PHONE,
    "email": ConversationAction.ASK_EMAIL,
    "company": ConversationAction.ASK_COMPANY,
}


@dataclass(frozen=True)
class ConversationDecision:
    action: ConversationAction = ConversationAction.ANSWER
    intent: str = ""
    pending_fields: list[str] = field(default_factory=list)
    commercial_intent: bool = False
    collection_active: bool = False
    rag_required: bool = False
    should_finalize: bool = False
    known_fields: dict[str, str] = field(default_factory=dict)
    response_source: str = "fallback"

    def to_prompt_payload(self) -> dict:
        return {
            "action": self.action.value,
            "intent": self.intent,
            "pending_fields": list(self.pending_fields),
            "commercial_intent": self.commercial_intent,
            "collection_active": self.collection_active,
            "rag_required": self.rag_required,
            "should_finalize": self.should_finalize,
            "known_fields": dict(self.known_fields),
            "response_source": self.response_source,
        }


def action_for_pending_fields(pending_fields: list[str], *, complete: bool = False) -> ConversationAction:
    if complete:
        return ConversationAction.COMPLETE_HANDOFF
    if not pending_fields:
        return ConversationAction.ANSWER
    return FIELD_ACTIONS.get(str(pending_fields[0] or ""), ConversationAction.ANSWER)


def known_lead_fields(lead) -> dict[str, str]:
    known: dict[str, str] = {}
    for field_name in ("name", "phone", "email", "company", "need_summary", "city"):
        value = str(getattr(lead, field_name, "") or "").strip()
        if value:
            known[field_name] = value[:200]
    return known
