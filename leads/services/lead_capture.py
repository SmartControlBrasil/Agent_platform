from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

import logging

from django.db import transaction

from assistant_core.conversation_turns import (
    build_enrichment_reply,
    skip_name_prompt_fields,
)
from assistant_core.qualification import (
    ContactSnapshot,
    extract_contact_snapshot,
    is_valid_city,
    is_valid_company,
    is_valid_email,
    is_valid_name,
    is_valid_need_summary,
    is_valid_phone,
    looks_like_invalid_email,
    looks_like_invalid_phone,
    minimum_lead_data_met,
)
from assistant_core.state import LeadState, next_state_after_message
from conversations.models import Conversation

from ..models import LeadDraft
from .commercial import QualificationPolicy, QualificationService, name_or_company_satisfied

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeadCaptureResult:
    lead_draft: LeadDraft
    missing_fields: list[str]
    is_qualified: bool
    extracted_snapshot: ContactSnapshot
    invalid_fields: list[str] = field(default_factory=list)
    state: str = LeadState.DISCOVERY


class LeadCaptureService:
    REQUIRED_FIELDS = ("name", "phone", "email", "need_summary")
    GENERIC_NEED_PHRASES = (
        "quero orcamento",
        "quero orçamento",
        "preciso de orcamento",
        "preciso de orçamento",
        "quanto custa",
        "valor",
        "proposta",
        "orcamento",
        "orçamento",
    )
    NEED_CONTEXT_KEYWORDS = (
        "preciso",
        "quero",
        "orçamento",
        "orcamento",
        "problema",
        "erro",
        "falha",
        "automação",
        "automacao",
        "sistema",
        "plataforma",
        "suporte",
        "manutenção",
        "manutencao",
        "integração",
        "integracao",
        "projeto",
        "desenvolver",
    )

    def __init__(self, qualification_service: QualificationService | None = None):
        self.qualification_service = qualification_service or QualificationService()

    def get_or_create_lead_draft(self, conversation: Conversation) -> LeadDraft:
        lead_draft, _ = LeadDraft.objects.get_or_create(
            conversation=conversation,
            defaults={"tenant": conversation.tenant},
        )
        return lead_draft

    @transaction.atomic
    def capture_from_message(
        self,
        conversation: Conversation,
        message: str,
        history: Iterable[dict[str, str]] | None = None,
        policy: QualificationPolicy | None = None,
    ) -> LeadCaptureResult:
        corpus = self._build_corpus(history=history, message=message)
        snapshot = extract_contact_snapshot(corpus)
        outcome = self.qualification_service.qualify_from_message(
            conversation=conversation,
            message=message,
            history=history,
            policy=policy,
        )

        if outcome.is_qualified:
            self._dispatch_webhook_lead_qualified(outcome.lead_draft)

        state_snapshot = next_state_after_message(
            conversation,
            outcome.lead_draft,
            intent="",
            extracted_data=snapshot,
        )
        self._sync_conversation(conversation, outcome.lead_draft, state_snapshot.state)

        return LeadCaptureResult(
            lead_draft=outcome.lead_draft,
            missing_fields=outcome.missing_fields,
            is_qualified=outcome.is_qualified,
            extracted_snapshot=snapshot,
            invalid_fields=outcome.invalid_fields,
            state=state_snapshot.state,
        )

    def _dispatch_webhook_lead_qualified(self, lead_draft: LeadDraft) -> None:
        from integrations.outbox.service import enqueue_lead_qualified

        event, created = enqueue_lead_qualified(lead_draft)
        logger.info(
            "outbox_enqueue lead_draft_id=%s tenant_slug=%s event_id=%s created=%s",
            lead_draft.id,
            lead_draft.tenant.slug,
            event.event_id,
            created,
        )

    def calculate_missing_fields(self, lead_draft: LeadDraft) -> list[str]:
        return self.qualification_service.missing_fields(lead_draft)

    def build_next_prompt(
        self,
        lead_draft: LeadDraft,
        missing_fields: list[str],
        intent: str = "",
        invalid_fields: list[str] | None = None,
    ) -> str:
        invalid_fields = invalid_fields or []
        prompts = {
            "name": "Qual é o seu nome?",
            "phone": "Qual é o melhor telefone ou WhatsApp para contato?",
            "email": "Qual é o seu e-mail?",
            "company": "Qual empresa você representa, se for o caso?",
            "need_summary": "Em uma frase, o que você precisa ou qual problema está enfrentando?",
        }
        if missing_fields and missing_fields[0] in prompts:
            field = missing_fields[0]
            if field == "name" and "name" in invalid_fields:
                return "Pode me informar seu nome real?"
            if field == "email" and "email" in invalid_fields:
                return "Esse e-mail parece incompleto. Pode me enviar novamente?"
            if field == "phone" and "phone" in invalid_fields:
                return "Esse telefone ficou incompleto. Pode enviar o telefone com DDD?"
            return prompts[field]
        missing_fields = skip_name_prompt_fields(missing_fields, lead_draft)
        if name_or_company_satisfied(lead_draft):
            invalid_fields = [field for field in invalid_fields if field not in {"name", "company"}]
        if "name" in invalid_fields and "name_or_company" in missing_fields:
            return "Para eu registrar certinho, me passa seu nome real, por favor."
        if "company" in invalid_fields and "name_or_company" in missing_fields:
            return "Pode me passar o nome real da empresa? Assim eu deixo o atendimento bem encaminhado."
        if "phone" in invalid_fields:
            return "Esse telefone ficou incompleto. Me envia um WhatsApp com DDD, por favor."
        if "email" in invalid_fields:
            return "Esse e-mail não parece válido. Pode me enviar um e-mail completo?"
        if "need_summary" in invalid_fields:
            return "Claro. Me conta rapidinho do que você precisa e em qual contexto para eu direcionar melhor."

        if not missing_fields:
            has_name_or_company = bool(
                (str(lead_draft.name or "").strip() and is_valid_name(lead_draft.name))
                or (str(lead_draft.company or "").strip() and is_valid_company(lead_draft.company))
            )
            if not has_name_or_company:
                return build_enrichment_reply(lead_draft)
            return "Perfeito. Já tenho os dados essenciais para seguir com o atendimento."
        if intent == "budget" and "need_summary" in missing_fields:
            return "Perfeito. Antes do contato, me conta em uma frase qual é a sua necessidade principal."

        next_field = missing_fields[0]
        if next_field == "need_summary":
            return "Perfeito. Me conta em uma frase qual é a sua necessidade principal."
        if next_field == "name_or_company":
            return "Ótimo. Para eu dar sequência, qual é o seu nome ou o nome da empresa?"
        if next_field == "phone_or_email":
            return "Entendi. Me passa seu telefone/WhatsApp ou e-mail para eu continuar."
        return "Perfeito. Pode me passar mais um dado para eu continuar?"

    def _sync_conversation(self, conversation: Conversation, lead_draft: LeadDraft, state: str) -> None:
        changed_fields: list[str] = []
        field_values = {
            "visitor_name": lead_draft.name or lead_draft.company,
            "visitor_email": lead_draft.email,
            "visitor_phone": lead_draft.phone,
        }
        for field_name, value in field_values.items():
            if not hasattr(conversation, field_name):
                continue
            current_value = str(getattr(conversation, field_name, "") or "").strip()
            next_value = str(value or "").strip()
            if next_value and current_value != next_value:
                setattr(conversation, field_name, next_value)
                changed_fields.append(field_name)

        if hasattr(conversation, "is_qualified"):
            is_qualified = lead_draft.status in {LeadDraft.Status.QUALIFIED, LeadDraft.Status.SENT_TO_CRM}
            if conversation.is_qualified != is_qualified:
                conversation.is_qualified = is_qualified
                changed_fields.append("is_qualified")

        if hasattr(conversation, "lead_state") and conversation.lead_state != state:
            conversation.lead_state = state
            changed_fields.append("lead_state")

        if changed_fields:
            conversation.save(update_fields=changed_fields + ["updated_at"])

    def _has_name_or_company(self, lead_draft: LeadDraft) -> bool:
        return bool(
            (str(lead_draft.name or "").strip() and is_valid_name(lead_draft.name))
            or (str(lead_draft.company or "").strip() and is_valid_company(lead_draft.company))
        )

    def _has_phone_or_email(self, lead_draft: LeadDraft) -> bool:
        return bool(
            (str(lead_draft.phone or "").strip() and is_valid_phone(lead_draft.phone))
            or (str(lead_draft.email or "").strip() and is_valid_email(lead_draft.email))
        )

    def _has_need_summary(self, lead_draft: LeadDraft) -> bool:
        return is_valid_need_summary(lead_draft.need_summary)

    def _build_corpus(self, history: Iterable[dict[str, str]] | None, message: str) -> str:
        parts = [
            str(item.get("content") or "")
            for item in (history or [])
            if item.get("role") == "user"
        ]
        parts.append(str(message or ""))
        return " ".join(part for part in parts if part.strip())

    def _extract_need_summary(
        self,
        *,
        history: Iterable[dict[str, str]] | None,
        message: str,
    ) -> str:
        candidate = self._select_need_summary_candidate(history=history, message=message)
        text = self._strip_contact_noise(candidate)
        if not is_valid_need_summary(text):
            return ""
        return text[:500].strip()

    def _strip_contact_noise(self, text: str) -> str:
        cleaned = str(text or "").strip()
        cleaned = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?:\+?\d[\d\s().-]{8,}\d)", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip(" ,.;:-")

    def _merge_text(self, current: str, new_value: str) -> str:
        current_value = str(current or "").strip()
        new_value = str(new_value or "").strip()
        if new_value:
            return new_value
        return current_value

    def _merge_validated(self, lead_draft: LeadDraft, field_name: str, new_value: str, validator, invalid_fields: list[str]) -> None:
        current_value = str(getattr(lead_draft, field_name, "") or "").strip()
        candidate = str(new_value or "").strip()
        if not candidate:
            return
        if not validator(candidate):
            self._append_invalid(invalid_fields, field_name)
            return
        if current_value and validator(current_value):
            return
        setattr(lead_draft, field_name, candidate)

    def _append_invalid(self, invalid_fields: list[str], field_name: str) -> None:
        if field_name not in invalid_fields:
            invalid_fields.append(field_name)

    def _is_generic_need_summary(self, text: str) -> bool:
        normalized = str(text or "").strip().lower()
        return any(phrase == normalized for phrase in self.GENERIC_NEED_PHRASES)

    def _message_is_vague_need(self, text: str) -> bool:
        cleaned = str(text or "").strip()
        return bool(cleaned and self._is_generic_need_summary(cleaned))

    def _select_need_summary_candidate(
        self,
        *,
        history: Iterable[dict[str, str]] | None,
        message: str,
    ) -> str:
        current_message = str(message or "").strip()
        if self._is_need_summary_candidate(current_message):
            return current_message

        for item in reversed(list(history or [])):
            if item.get("role") != "user":
                continue
            content = str(item.get("content") or "").strip()
            if self._is_need_summary_candidate(content):
                return content
        return ""

    def _is_need_summary_candidate(self, text: str) -> bool:
        cleaned = str(text or "").strip()
        if not cleaned:
            return False
        if self._is_generic_need_summary(cleaned):
            return False
        lowered = cleaned.lower()
        snapshot = extract_contact_snapshot(cleaned)
        if snapshot.has_any_contact() and not any(keyword in lowered for keyword in self.NEED_CONTEXT_KEYWORDS):
            return False
        return is_valid_need_summary(cleaned)
