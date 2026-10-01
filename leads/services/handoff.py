from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.db import connection, transaction
from django.utils import timezone

from assistant_core.qualification import extract_contact_snapshot, normalize_text
from audit.services import record_audit_event
from assistant_core.summary import build_conversation_summary, format_conversation_summary_notes
from conversations.models import HandoffRequest
from leads.models import LeadDraft

from .commercial import ACTION_HANDOFF_COMPLETED, ACTION_HANDOFF_REQUESTED
from .handoff_notification import HandoffNotificationService

logger = logging.getLogger(__name__)


EXPLICIT_HANDOFF_PATTERNS = (
    "falar com alguem",
    "falar com alguém",
    "falar com humano",
    "quero atendimento humano",
    "atendimento humano",
    "falar com uma pessoa",
    "quero falar com uma pessoa",
    "falar com atendente",
    "atendente",
    "me passa para um especialista",
    "chama um vendedor",
    "chamar um vendedor",
    "vendedor",
    "consultor",
    "especialista",
    "me liga",
    "me ligue",
    "liga pra mim",
    "entrar em contato",
    "entrasse em contato",
    "entre em contato",
    "entrem em contato",
    "alguem entre em contato",
    "alguem entrasse em contato",
    "quero contato",
    "pessoa de verdade",
)
URGENT_PATTERNS = (
    "urgente",
    "emergencia",
    "emergência",
    "parou",
    "parada",
    "sem funcionar",
    "fora do ar",
    "risco",
    "queimado",
    "cheiro de queimado",
    "vazamento",
    "agora mesmo",
    "preciso agora",
    "resolver agora",
    "atender agora",
)
COMPLEX_TECHNICAL_PATTERNS = (
    "sem comunicacao",
    "sem comunicação",
    "falha intermitente",
    "linha parada",
    "maquina parada",
    "máquina parada",
)


@dataclass(frozen=True)
class HandoffDecision:
    should_create: bool
    reason: str = ""
    priority: str = HandoffRequest.Priority.NORMAL


@dataclass(frozen=True)
class HandoffServiceResult:
    handoff: HandoffRequest | None
    created: bool = False
    notification_result: object | None = None


class HandoffService:
    active_statuses = {HandoffRequest.Status.PENDING, HandoffRequest.Status.SENT}

    def __init__(self, notification_service: HandoffNotificationService | None = None):
        self.notification_service = notification_service or HandoffNotificationService()

    def should_create_handoff(self, conversation, lead_draft, discovery_result, message) -> HandoffDecision:
        normalized = normalize_text(message)
        service_area = getattr(discovery_result, "service_area", "unknown") if discovery_result is not None else "unknown"
        intent = getattr(discovery_result, "intent", "unknown") if discovery_result is not None else "unknown"

        # Recusa de contato / "tire minhas dúvidas" não é gatilho de handoff.
        try:
            from assistant_core.dialogue_memory import is_contact_deferred, wants_consultative_continue

            if is_contact_deferred(message) or wants_consultative_continue(message):
                return HandoffDecision(False)
        except Exception:
            pass

        if self._has_explicit_request(normalized):
            priority = HandoffRequest.Priority.HIGH if self._is_urgent(normalized) else HandoffRequest.Priority.NORMAL
            return HandoffDecision(True, HandoffRequest.Reason.EXPLICIT_REQUEST, priority)

        if lead_draft is not None and lead_draft.status in {LeadDraft.Status.QUALIFIED, LeadDraft.Status.SENT_TO_CRM}:
            try:
                from assistant_core.consultative_policy import is_consultative_need_discovery
                from assistant_core.services.decision_outcome import is_consultative_knowledge_turn

                is_consultative_turn = is_consultative_knowledge_turn(
                    discovery_result, message
                ) or is_consultative_need_discovery(discovery_result, message)
                if is_consultative_turn and not self._has_explicit_request(normalized):
                    return HandoffDecision(False)
            except Exception:
                pass
            return HandoffDecision(True, HandoffRequest.Reason.QUALIFIED_LEAD, HandoffRequest.Priority.NORMAL)

        if self._is_urgent(normalized):
            return HandoffDecision(True, HandoffRequest.Reason.EMERGENCY_OR_URGENT, HandoffRequest.Priority.HIGH)

        if self._is_complex_technical(normalized):
            return HandoffDecision(True, HandoffRequest.Reason.TECHNICAL_COMPLEXITY, HandoffRequest.Priority.HIGH)

        if intent == "support_request" and self._is_urgent(normalized):
            return HandoffDecision(True, HandoffRequest.Reason.SUPPORT_REQUEST, HandoffRequest.Priority.HIGH)

        return HandoffDecision(False)

    @transaction.atomic
    def create_or_update_handoff(self, conversation, lead_draft=None, discovery_result=None, message="") -> HandoffServiceResult:
        if conversation is None:
            return HandoffServiceResult(handoff=None)

        decision = self.should_create_handoff(conversation, lead_draft, discovery_result, message)
        if not decision.should_create:
            return HandoffServiceResult(handoff=None)

        handoff_queryset = HandoffRequest.objects.filter(
            tenant=conversation.tenant,
            conversation=conversation,
            status__in=self.active_statuses,
        ).order_by("-created_at")
        if connection.features.has_select_for_update:
            handoff_queryset = handoff_queryset.select_for_update()
        handoff = handoff_queryset.first()
        created = handoff is None
        if created:
            handoff = HandoffRequest(tenant=conversation.tenant, conversation=conversation)

        snapshot = extract_contact_snapshot(message)
        if lead_draft is None:
            lead_draft = getattr(conversation, "lead_draft", None)

        handoff.lead_draft = lead_draft
        if created or decision.reason == HandoffRequest.Reason.EXPLICIT_REQUEST or handoff.reason != HandoffRequest.Reason.EXPLICIT_REQUEST:
            handoff.reason = decision.reason
        handoff.priority = self._max_priority(getattr(handoff, "priority", ""), decision.priority)
        handoff.visitor_name = self._first(
            getattr(lead_draft, "name", ""),
            snapshot.name,
            getattr(conversation, "visitor_name", ""),
        )
        handoff.visitor_company = self._first(getattr(lead_draft, "company", ""), snapshot.company)
        handoff.visitor_phone = self._first(getattr(lead_draft, "phone", ""), snapshot.phone, getattr(conversation, "visitor_phone", ""))
        handoff.visitor_email = self._first(getattr(lead_draft, "email", ""), snapshot.email, getattr(conversation, "visitor_email", ""))
        handoff.source_page = str(getattr(conversation, "source_page", "") or "")
        handoff.summary = self._build_summary(conversation, lead_draft, message=message)
        handoff.handoff_state = HandoffRequest.HandoffState.REQUESTED
        handoff.dispatch_state = getattr(handoff, "dispatch_state", "") or HandoffRequest.DispatchState.PENDING
        handoff.metadata = {
            **(handoff.metadata or {}),
            "message_signal": self._message_signal(message),
            "service_area": getattr(discovery_result, "service_area", "unknown") if discovery_result is not None else "unknown",
            "intent": getattr(discovery_result, "intent", "unknown") if discovery_result is not None else "unknown",
        }
        handoff.save()

        notification_result = None
        if lead_draft is not None and getattr(lead_draft, "tenant_id", None) == conversation.tenant_id:
            lead_draft.handoff_status = LeadDraft.HandoffStatus.REQUESTED
            lead_draft.save(update_fields=["handoff_status", "updated_at"])
        if created:
            from integrations.outbox.service import enqueue_handoff_created

            event, enqueued = enqueue_handoff_created(handoff)
            logger.info(
                "outbox_enqueue handoff_id=%s tenant_slug=%s event_id=%s created=%s",
                handoff.id,
                handoff.tenant.slug,
                event.event_id,
                enqueued,
            )
            record_audit_event(
                action=ACTION_HANDOFF_REQUESTED,
                tenant=handoff.tenant,
                obj=handoff,
                after_data={
                    "handoff_state": handoff.handoff_state,
                    "dispatch_state": handoff.dispatch_state,
                    "reason": handoff.reason,
                    "priority": handoff.priority,
                },
                metadata={"outbox_event_id": str(event.event_id), "outbox_created": enqueued},
            )
        return HandoffServiceResult(handoff=handoff, created=created, notification_result=notification_result)

    def mark_sent(self, handoff):
        handoff.status = HandoffRequest.Status.SENT
        if getattr(handoff, "dispatch_state", "") == HandoffRequest.DispatchState.PENDING:
            handoff.dispatch_state = HandoffRequest.DispatchState.DELIVERED
        handoff.save(update_fields=["status", "dispatch_state", "updated_at"])
        return handoff

    def mark_resolved(self, handoff):
        handoff.status = HandoffRequest.Status.RESOLVED
        handoff.handoff_state = HandoffRequest.HandoffState.COMPLETED
        handoff.resolved_at = timezone.now()
        handoff.save(update_fields=["status", "handoff_state", "resolved_at", "updated_at"])
        if handoff.lead_draft_id and handoff.lead_draft.tenant_id == handoff.tenant_id:
            handoff.lead_draft.handoff_status = LeadDraft.HandoffStatus.COMPLETED
            handoff.lead_draft.save(update_fields=["handoff_status", "updated_at"])
        record_audit_event(
            action=ACTION_HANDOFF_COMPLETED,
            tenant=handoff.tenant,
            obj=handoff,
            after_data={"handoff_state": handoff.handoff_state, "status": handoff.status},
            metadata={"source": "handoff_service"},
        )
        return handoff

    def mark_cancelled(self, handoff):
        handoff.status = HandoffRequest.Status.CANCELLED
        handoff.handoff_state = HandoffRequest.HandoffState.CANCELLED
        handoff.save(update_fields=["status", "handoff_state", "updated_at"])
        if handoff.lead_draft_id and handoff.lead_draft.tenant_id == handoff.tenant_id:
            handoff.lead_draft.handoff_status = LeadDraft.HandoffStatus.CANCELLED
            handoff.lead_draft.save(update_fields=["handoff_status", "updated_at"])
        return handoff

    def _build_summary(self, conversation, lead_draft, message="") -> str:
        summary = format_conversation_summary_notes(build_conversation_summary(conversation, lead_draft=lead_draft))
        from assistant_core.summary import build_conversation_transcript

        transcript = build_conversation_transcript(conversation, lead_draft=lead_draft)
        if transcript:
            summary = f"{summary}\n\nHistórico da conversa:\n{transcript}"
        current_message = str(message or "").strip()
        if current_message and current_message not in summary:
            summary = f"{summary}\n- Última mensagem: {current_message[:500]}"
        return summary

    def _message_signal(self, message: str) -> str:
        normalized = normalize_text(message)
        if self._has_explicit_request(normalized):
            return "explicit_handoff_request"
        if self._is_urgent(normalized):
            return "urgent"
        if self._is_complex_technical(normalized):
            return "technical_complexity"
        return "business_rule"

    def _has_explicit_request(self, normalized: str) -> bool:
        from assistant_core.consultative_policy import is_explicit_human_handoff

        return is_explicit_human_handoff(normalized)

    def _is_urgent(self, normalized: str) -> bool:
        return any(pattern in normalized for pattern in URGENT_PATTERNS)

    def _is_complex_technical(self, normalized: str) -> bool:
        return any(pattern in normalized for pattern in COMPLEX_TECHNICAL_PATTERNS)

    def _first(self, *values) -> str:
        for value in values:
            if str(value or "").strip():
                return str(value).strip()
        return ""

    def _max_priority(self, current: str, new: str) -> str:
        order = {
            HandoffRequest.Priority.LOW: 1,
            HandoffRequest.Priority.NORMAL: 2,
            HandoffRequest.Priority.HIGH: 3,
            HandoffRequest.Priority.URGENT: 4,
        }
        return new if order.get(new, 0) >= order.get(current, 0) else current
