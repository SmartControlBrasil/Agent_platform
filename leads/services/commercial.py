from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from django.db import connection, transaction
from django.utils import timezone

from assistant_core.conversation_turns import (
    is_consultative_context_answer,
    is_direct_question,
    is_name_deferred,
    is_need_enrichment,
    mark_name_deferred,
    merge_need_summaries,
)
from assistant_core.consultative_policy import COLLECTION_ACTIVE_KEY
from assistant_core.dialogue_memory import (
    ACTIVE_DOMAIN_KEY,
    ACTIVE_ENTITY_KEY,
    ACTIVE_NEED_KEY,
    ACTIVE_TOPIC_KEY,
    COLLECTION_PAUSED_KEY,
    COMMERCIAL_INTENT_KEY,
    CONTACT_DEFERRED_KEY,
)
from assistant_core.qualification import (
    extract_contact_snapshot,
    infer_pending_field_values,
    is_valid_city,
    is_valid_company,
    is_valid_email,
    is_valid_name,
    is_valid_need_summary,
    is_valid_phone,
    looks_like_invalid_email,
    looks_like_invalid_phone,
    message_is_plausible_phone_candidate,
    normalize_text,
)
from audit.services import record_audit_event
from conversations.models import Conversation, HandoffRequest
from integrations.models import OutboxEvent
from leads.models import LeadDraft
from tenants.models import (
    HUMAN_HANDOFF_CHANNEL_WHATSAPP,
    MAX_WHATSAPP_NUMBER_LENGTH,
    MIN_WHATSAPP_NUMBER_LENGTH,
    normalize_whatsapp_number,
)

FIELD_SOURCE_EXPLICIT = "explicitly_provided"
FIELD_SOURCE_NORMALIZED = "normalized"
FIELD_SOURCE_DERIVED = "derived"
FIELD_SOURCE_INFERRED = "inferred"
FIELD_SOURCE_UNKNOWN = "unknown"

COLLECTION_DEFERRAL_PHRASES = frozenset(
    {
        "ja passei",
        "já passei",
        "ja falei",
        "já falei",
        "ja informei",
        "já informei",
        "ja disse",
        "já disse",
        "ja dei",
        "já dei",
        "ja mandei",
        "já mandei",
    }
)


def is_collection_deferral_phrase(text: str) -> bool:
    normalized = normalize_text(text)
    return normalized in COLLECTION_DEFERRAL_PHRASES


def name_or_company_satisfied(lead: LeadDraft | None) -> bool:
    if lead is None:
        return False
    return bool(
        (str(getattr(lead, "name", "") or "").strip() and is_valid_name(getattr(lead, "name", "")))
        or (str(getattr(lead, "company", "") or "").strip() and is_valid_company(getattr(lead, "company", "")))
    )


def resolve_lead_draft(conversation=None, lead_draft=None) -> LeadDraft | None:
    """Retorna lead atualizado do banco — evita cache stale em conversation.lead_draft."""
    if lead_draft is not None and getattr(lead_draft, "pk", None):
        try:
            lead_draft.refresh_from_db()
            return lead_draft
        except Exception:
            pass
    if conversation is None:
        return lead_draft
    try:
        return LeadDraft.objects.filter(conversation=conversation).first()
    except Exception:
        return None


ACTION_LEAD_CREATED = "lead.created"
ACTION_LEAD_UPDATED = "lead.updated"
ACTION_QUALIFICATION_PROGRESSED = "qualification.progressed"
ACTION_LEAD_QUALIFIED = "lead.qualified"
ACTION_HANDOFF_READY = "handoff.ready"
ACTION_HANDOFF_REQUESTED = "handoff.requested"
ACTION_HANDOFF_COMPLETED = "handoff.completed"
ACTION_DISPATCH_QUEUED = "dispatch.queued"
ACTION_DISPATCH_FAILED = "dispatch.failed"

COMMON_FIELDS = ("name", "phone", "email", "company", "city", "interest", "need_summary")

# Chaves de payload operacional em qualification_data (não são custom fields de formulário).
OPERATIONAL_QUALIFICATION_KEYS = frozenset(
    {
        COLLECTION_ACTIVE_KEY,
        COLLECTION_PAUSED_KEY,
        CONTACT_DEFERRED_KEY,
        COMMERCIAL_INTENT_KEY,
        ACTIVE_ENTITY_KEY,
        ACTIVE_DOMAIN_KEY,
        ACTIVE_TOPIC_KEY,
        ACTIVE_NEED_KEY,
        "name_deferred",
        "collection_trigger_reason",
        "relational_name_asked",
        "relational_name_captured",
        "contact_reason_shown",
        "privacy_notice_shown",
        "company_not_applicable",
    }
)

# Universo conhecido de custom fields de policy — desconhecidos fora da policy são descartados.
KNOWN_CUSTOM_FIELD_KEYS = frozenset(
    {
        "material",
        "ambiente",
        "prazo",
        "instituicao",
        "aplicacao",
        "equipamento",
    }
)
CONTACT_FIELDS = {"name", "phone", "email", "company"}
SOURCE_STRENGTH = {
    FIELD_SOURCE_UNKNOWN: 0,
    FIELD_SOURCE_INFERRED: 1,
    FIELD_SOURCE_DERIVED: 2,
    FIELD_SOURCE_NORMALIZED: 3,
    FIELD_SOURCE_EXPLICIT: 4,
}


@dataclass(frozen=True)
class QualificationFieldSpec:
    key: str
    label: str
    required: bool = False
    common_field: str = ""
    max_length: int = 160
    validator: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class QualificationPolicy:
    slug: str = "default"
    desired_fields: tuple[str, ...] = ("name", "phone", "email", "company", "need_summary", "city")
    required_fields: tuple[str, ...] = ("name", "phone", "email", "need_summary")
    custom_fields: tuple[QualificationFieldSpec, ...] = field(default_factory=tuple)
    allow_early_handoff: bool = True

    @classmethod
    def for_tenant(cls, tenant) -> "QualificationPolicy":
        try:
            profile = tenant.assistant_profile
        except Exception:
            profile = None
        slug = str(getattr(tenant, "slug", "") or "default")[:80]
        return cls(slug=slug, allow_early_handoff=bool(getattr(profile, "human_handoff_enabled", False)))

    @property
    def allowed_custom_keys(self) -> set[str]:
        return {spec.key for spec in self.custom_fields}


@dataclass(frozen=True)
class QualificationOutcome:
    lead_draft: LeadDraft
    state: str
    collected_fields: dict[str, str]
    missing_fields: list[str]
    invalid_fields: list[str]
    is_qualified: bool
    can_request_handoff: bool
    next_action: str
    handoff_reason: str = ""


@dataclass(frozen=True)
class CommercialReadiness:
    status: str
    details: dict


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if digits.startswith("55") and len(digits) in {12, 13}:
        digits = digits[2:]
    return digits[:40]


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()[:254]


def normalize_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    return cleaned.strip(" .,-")[:120]


def normalize_city(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:120]


def merge_field_value(lead: LeadDraft, field_name: str, value: str, *, source: str) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return False
    current = str(getattr(lead, field_name, "") or "").strip()
    sources = dict(lead.field_sources or {})
    current_source = str(sources.get(field_name) or FIELD_SOURCE_UNKNOWN)
    if current and SOURCE_STRENGTH.get(current_source, 0) > SOURCE_STRENGTH.get(source, 0):
        return False
    if current and current == candidate:
        sources[field_name] = max_source(current_source, source)
        lead.field_sources = sources
        return False
    setattr(lead, field_name, candidate)
    sources[field_name] = max_source(current_source, source)
    lead.field_sources = sources
    return True


def max_source(left: str, right: str) -> str:
    return left if SOURCE_STRENGTH.get(left, 0) >= SOURCE_STRENGTH.get(right, 0) else right


class QualificationService:
    def __init__(self, policy: QualificationPolicy | None = None):
        self.policy = policy

    @transaction.atomic
    def qualify_from_message(self, *, conversation: Conversation, message: str, history=None, policy: QualificationPolicy | None = None) -> QualificationOutcome:
        policy = policy or self.policy or QualificationPolicy.for_tenant(conversation.tenant)
        lead, created = self._get_or_create_locked_lead(conversation)
        before = self._state_snapshot(lead)
        changed = False
        invalid_fields: list[str] = []
        corpus = self._build_corpus(history=history, message=message)
        snapshot = extract_contact_snapshot(corpus)
        current_snapshot = extract_contact_snapshot(message)

        if is_name_deferred(message):
            mark_name_deferred(lead)

        changed |= self._merge_common(lead, "name", snapshot.name, normalize_name, is_valid_name, invalid_fields)
        changed |= self._merge_common(lead, "company", snapshot.company, normalize_name, is_valid_company, invalid_fields)
        changed |= self._merge_common(lead, "email", snapshot.email, normalize_email, is_valid_email, invalid_fields)
        changed |= self._merge_common(lead, "phone", snapshot.phone, normalize_phone, is_valid_phone, invalid_fields)
        changed |= self._merge_common(lead, "city", snapshot.city, normalize_city, is_valid_city, invalid_fields)

        collection_active = bool((lead.qualification_data or {}).get(COLLECTION_ACTIVE_KEY))
        pending = self.promptable_fields(
            lead,
            history=history,
            message=message,
            policy=policy,
        )
        from assistant_core.qualification.livia import is_company_not_applicable
        if collection_active and pending and pending[0] == "company" and is_company_not_applicable(message):
            lead.qualification_data = {**(lead.qualification_data or {}), "company_not_applicable": True}
            changed = True
        if pending and collection_active:
            allow_infer = pending[0] == "need_summary" or not is_consultative_context_answer(message)
            if allow_infer:
                inferred = infer_pending_field_values(message, pending[0])
                validators = {
                    "name": (normalize_name, is_valid_name),
                    "company": (normalize_name, is_valid_company),
                    "email": (normalize_email, is_valid_email),
                    "phone": (normalize_phone, is_valid_phone),
                    "city": (normalize_city, is_valid_city),
                }
                for field_name, value in inferred.items():
                    if field_name == "need_summary":
                        changed |= merge_field_value(
                            lead,
                            "need_summary",
                            str(value)[:500],
                            source=FIELD_SOURCE_EXPLICIT,
                        )
                        continue
                    normalizer, validator = validators[field_name]
                    changed |= self._merge_common(lead, field_name, value, normalizer, validator, invalid_fields)

        need_summary = self._extract_need_summary(history=history, message=message, existing=lead.need_summary)
        if need_summary:
            changed |= merge_field_value(lead, "need_summary", need_summary[:500], source=FIELD_SOURCE_EXPLICIT)
        elif self._message_is_vague_need(message) and not is_valid_need_summary(lead.need_summary):
            _append_once(invalid_fields, "need_summary")
        if current_snapshot.email and not is_valid_email(current_snapshot.email):
            _append_once(invalid_fields, "email")
        elif looks_like_invalid_email(message):
            _append_once(invalid_fields, "email")
        if (
            current_snapshot.phone
            and message_is_plausible_phone_candidate(message)
            and not is_valid_phone(current_snapshot.phone)
        ):
            _append_once(invalid_fields, "phone")
        elif looks_like_invalid_phone(message):
            _append_once(invalid_fields, "phone")

        if collection_active and is_consultative_context_answer(message):
            from assistant_core.consultative_slots import extract_consultative_slots

            slots = extract_consultative_slots(
                need_summary=lead.need_summary,
                history=history,
                current_message=message,
            )
            if slots.environment_type or slots.area_size or slots.floor_surface:
                data = dict(lead.qualification_data or {})
                if slots.environment_type:
                    data["consultative_environment"] = True
                if slots.area_size:
                    data["consultative_area_size"] = True
                if slots.floor_surface:
                    data["consultative_floor_surface"] = True
                lead.qualification_data = data
                changed = True

        if is_valid_name(lead.name):
            invalid_fields = [field for field in invalid_fields if field != "name"]
        if is_valid_company(lead.company) or (lead.qualification_data or {}).get("company_not_applicable"):
            invalid_fields = [field for field in invalid_fields if field != "company"]

        changed |= self._merge_custom_fields(lead, policy=policy, message=message)
        lead.qualification_policy = policy.slug
        missing = self.missing_fields(lead, policy=policy)
        missing_required = self.missing_required_fields(lead, policy=policy)
        promptable = self.promptable_fields(
            lead,
            history=history,
            message=message,
            policy=policy,
        )
        lead.qualification_status = (
            LeadDraft.QualificationStatus.QUALIFIED
            if not missing_required
            else LeadDraft.QualificationStatus.IN_PROGRESS
        )
        if lead.status not in {LeadDraft.Status.SENT_TO_CRM, LeadDraft.Status.FAILED}:
            lead.status = LeadDraft.Status.QUALIFIED if not missing_required else LeadDraft.Status.DRAFT
        if not missing_required and lead.handoff_status == LeadDraft.HandoffStatus.NOT_REQUESTED:
            lead.handoff_status = LeadDraft.HandoffStatus.READY
        if lead.dispatch_status == LeadDraft.DispatchStatus.NOT_QUEUED and lead.status == LeadDraft.Status.QUALIFIED:
            lead.dispatch_status = LeadDraft.DispatchStatus.PENDING
        lead.save()
        self._sync_conversation(conversation, lead)
        after = self._state_snapshot(lead)
        self._audit(created=created, lead=lead, before=before, after=after, missing=missing, changed=changed)
        return QualificationOutcome(
            lead_draft=lead,
            state=lead.qualification_status,
            collected_fields=self.collected_fields(lead),
            missing_fields=promptable if collection_active else missing,
            invalid_fields=invalid_fields,
            is_qualified=lead.qualification_status == LeadDraft.QualificationStatus.QUALIFIED,
            can_request_handoff=self.can_request_handoff(lead, policy=policy),
            next_action="handoff_ready" if not missing else f"collect:{missing[0]}",
            handoff_reason=HandoffRequest.Reason.QUALIFIED_LEAD if not missing_required else "",
        )

    def _get_or_create_locked_lead(self, conversation: Conversation) -> tuple[LeadDraft, bool]:
        queryset = LeadDraft.objects.filter(tenant=conversation.tenant, conversation=conversation)
        if connection.features.has_select_for_update:
            queryset = queryset.select_for_update()
        lead = queryset.first()
        if lead is not None:
            return lead, False
        return LeadDraft.objects.create(tenant=conversation.tenant, conversation=conversation), True

    def missing_required_fields(self, lead: LeadDraft, *, policy: QualificationPolicy | None = None) -> list[str]:
        """Campos obrigatórios para qualificação material do lead (company não entra aqui)."""
        policy = policy or self.policy or QualificationPolicy.for_tenant(lead.tenant)
        missing = []
        for field_name in policy.required_fields:
            validator = {"name": is_valid_name, "phone": is_valid_phone, "email": is_valid_email}.get(field_name)
            if validator:
                if not validator(getattr(lead, field_name, "")):
                    missing.append(field_name)
                continue
            if field_name == "name_or_company" and not ((lead.name and is_valid_name(lead.name)) or (lead.company and is_valid_company(lead.company))):
                missing.append(field_name)
            elif field_name == "phone_or_email" and not ((lead.phone and is_valid_phone(lead.phone)) or (lead.email and is_valid_email(lead.email))):
                missing.append(field_name)
            elif field_name == "need_summary" and not is_valid_need_summary(lead.need_summary):
                missing.append(field_name)
            elif field_name.startswith("custom:"):
                key = field_name.split(":", 1)[1]
                if not str((lead.qualification_data or {}).get(key) or "").strip():
                    missing.append(field_name)
            elif field_name in COMMON_FIELDS and not str(getattr(lead, field_name, "") or "").strip():
                missing.append(field_name)
        return missing

    def missing_fields(self, lead: LeadDraft, *, policy: QualificationPolicy | None = None) -> list[str]:
        """Campos materialmente ausentes para qualificação (company não entra aqui)."""
        policy = policy or self.policy or QualificationPolicy.for_tenant(lead.tenant)
        return list(self.missing_required_fields(lead, policy=policy))

    def possible_enrichment_fields(
        self,
        lead: LeadDraft,
        *,
        history=None,
        message: str = "",
        policy: QualificationPolicy | None = None,
    ) -> list[str]:
        """Campos opcionais que podem ser perguntados quando o contexto justifica."""
        policy = policy or self.policy or QualificationPolicy.for_tenant(lead.tenant)
        if "company" not in policy.desired_fields:
            return []
        if is_valid_company(lead.company) or (lead.qualification_data or {}).get("company_not_applicable"):
            return []
        from assistant_core.relational_collection import has_b2b_context

        if has_b2b_context(lead=lead, history=history, message=message):
            return ["company"]
        return []

    def promptable_fields(
        self,
        lead: LeadDraft,
        *,
        history=None,
        message: str = "",
        policy: QualificationPolicy | None = None,
    ) -> list[str]:
        """Campos a perguntar agora — somente com coleta comercial ativa."""
        from assistant_core.relational_collection import pending_collection_fields

        if not (lead.qualification_data or {}).get(COLLECTION_ACTIVE_KEY):
            return []
        return pending_collection_fields(lead=lead, history=history, message=message)

    def collected_fields(self, lead: LeadDraft) -> dict[str, str]:
        data = {}
        for field_name in COMMON_FIELDS:
            value = str(getattr(lead, field_name if field_name != "interest" else "need_summary", "") or "").strip()
            if value:
                data[field_name] = (lead.field_sources or {}).get(field_name, FIELD_SOURCE_UNKNOWN)
        for key, value in (lead.qualification_data or {}).items():
            if str(value or "").strip():
                data[f"custom:{key}"] = (lead.field_sources or {}).get(f"custom:{key}", FIELD_SOURCE_UNKNOWN)
        return data

    def can_request_handoff(self, lead: LeadDraft, *, policy: QualificationPolicy | None = None, explicit_request: bool = False) -> bool:
        policy = policy or self.policy or QualificationPolicy.for_tenant(lead.tenant)
        return explicit_request and policy.allow_early_handoff or not self.missing_required_fields(lead, policy=policy)

    def _merge_common(self, lead, field_name, value, normalizer, validator, invalid_fields) -> bool:
        candidate = normalizer(value)
        if not candidate:
            return False
        if not validator(candidate):
            _append_once(invalid_fields, field_name)
            return False
        return merge_field_value(lead, field_name, candidate, source=FIELD_SOURCE_EXPLICIT)

    def _merge_custom_fields(self, lead, *, policy: QualificationPolicy, message: str) -> bool:
        changed = False
        data = dict(lead.qualification_data or {})
        sources = dict(lead.field_sources or {})
        normalized_message = normalize_text(message)
        for spec in policy.custom_fields:
            value = _extract_labeled_value(message, spec.label or spec.key) or _extract_labeled_value(message, spec.key)
            if not value and spec.key in {"material", "ambiente", "prazo", "instituicao", "aplicacao", "equipamento"}:
                value = _extract_after_keyword(message, spec.key)
            value = str(value or "").strip()[: spec.max_length]
            if not value:
                continue
            if spec.validator and not spec.validator(value):
                continue
            current = str(data.get(spec.key) or "").strip()
            if current:
                continue
            data[spec.key] = value
            sources[f"custom:{spec.key}"] = FIELD_SOURCE_EXPLICIT if normalize_text(value) in normalized_message else FIELD_SOURCE_DERIVED
            changed = True
        if changed:
            allowed = policy.allowed_custom_keys
            # Mantém metadados operacionais e customs da policy; remove só customs fora da policy.
            lead.qualification_data = {
                key: value
                for key, value in data.items()
                if (
                    key in allowed
                    or key in OPERATIONAL_QUALIFICATION_KEYS
                    or key.startswith("lead_")
                    or key not in KNOWN_CUSTOM_FIELD_KEYS
                )
            }
            lead.field_sources = sources
        return changed

    def _sync_conversation(self, conversation: Conversation, lead: LeadDraft) -> None:
        fields = []
        updates = {"visitor_name": lead.name or lead.company, "visitor_email": lead.email, "visitor_phone": lead.phone}
        for field_name, value in updates.items():
            if value and getattr(conversation, field_name) != value:
                setattr(conversation, field_name, value)
                fields.append(field_name)
        qualified = lead.qualification_status == LeadDraft.QualificationStatus.QUALIFIED
        if conversation.is_qualified != qualified:
            conversation.is_qualified = qualified
            fields.append("is_qualified")
        if fields:
            conversation.save(update_fields=fields + ["updated_at"])

    def _build_corpus(self, *, history, message: str) -> str:
        parts = [str(item.get("content") or "") for item in (history or []) if item.get("role") == "user"]
        parts.append(str(message or ""))
        return " ".join(part for part in parts if part.strip())

    def _extract_need_summary(self, *, history, message: str, existing: str = "") -> str:
        from assistant_core.consultative_policy import is_explicit_human_handoff

        current = self._strip_contact_noise(message)
        preserved = str(existing or "").strip()
        if is_explicit_human_handoff(current):
            return preserved[:500]
        history_need = ""
        for candidate in [
            self._strip_contact_noise(item.get("content") or "")
            for item in (history or [])
            if item.get("role") == "user"
        ]:
            text = str(candidate or "").strip()
            if is_valid_need_summary(text):
                history_need = merge_need_summaries(history_need, text)
        base = merge_need_summaries(preserved, history_need)
        if is_name_deferred(current) or is_direct_question(current):
            return base[:500]
        if is_need_enrichment(current) or is_valid_need_summary(current):
            return merge_need_summaries(base, current)
        return base[:500]

    def _strip_contact_noise(self, text: str) -> str:
        cleaned = str(text or "").strip()
        cleaned = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?:\+?\d[\d\s().-]{8,}\d)", "", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip(" ,.;:-")

    def _message_is_vague_need(self, text: str) -> bool:
        normalized = normalize_text(text)
        return normalized in {"quero orcamento", "quero orçamento", "preciso de orcamento", "preciso de orçamento", "quanto custa", "valor", "proposta", "orcamento", "orçamento"}

    def _state_snapshot(self, lead: LeadDraft) -> dict:
        return {
            "qualification_status": lead.qualification_status,
            "handoff_status": lead.handoff_status,
            "dispatch_status": lead.dispatch_status,
            "status": lead.status,
            "collected_fields": sorted(self.collected_fields(lead)),
        }

    def _audit(self, *, created: bool, lead: LeadDraft, before: dict, after: dict, missing: list[str], changed: bool) -> None:
        action = ACTION_LEAD_CREATED if created else ACTION_LEAD_UPDATED
        if created or changed or before != after:
            record_audit_event(action=action, tenant=lead.tenant, obj=lead, before_data=before if not created else {}, after_data=after, metadata={"missing_fields": missing, "source": "qualification_service"})
        if before.get("qualification_status") != after.get("qualification_status"):
            progressed = ACTION_LEAD_QUALIFIED if after.get("qualification_status") == LeadDraft.QualificationStatus.QUALIFIED else ACTION_QUALIFICATION_PROGRESSED
            record_audit_event(action=progressed, tenant=lead.tenant, obj=lead, before_data=before, after_data=after, metadata={"missing_fields": missing})
        if before.get("handoff_status") != after.get("handoff_status") and after.get("handoff_status") == LeadDraft.HandoffStatus.READY:
            record_audit_event(action=ACTION_HANDOFF_READY, tenant=lead.tenant, obj=lead, before_data=before, after_data=after, metadata={"source": "qualification_service"})


class CommercialReadinessService:
    def readiness(self, *, tenant) -> CommercialReadiness:
        policy = QualificationPolicy.for_tenant(tenant)
        try:
            profile = tenant.assistant_profile
        except Exception:
            profile = None
        whatsapp_number = normalize_whatsapp_number(getattr(profile, "handoff_whatsapp_number", "")) if profile else ""
        whatsapp_ready = bool(
            profile
            and getattr(profile, "human_handoff_enabled", False)
            and getattr(profile, "human_handoff_channel", "") == HUMAN_HANDOFF_CHANNEL_WHATSAPP
            and MIN_WHATSAPP_NUMBER_LENGTH <= len(whatsapp_number) <= MAX_WHATSAPP_NUMBER_LENGTH
        )
        outbox_ready = _table_available(OutboxEvent)
        details = {
            "qualification_policy": policy.slug,
            "required_fields": list(policy.required_fields),
            "handoff_configured": whatsapp_ready,
            "outbox_available": outbox_ready,
            "smart360_real_enabled": False,
        }
        if not policy.required_fields:
            status = "NOT_CONFIGURED"
        elif whatsapp_ready and outbox_ready:
            status = "READY"
        elif outbox_ready:
            status = "PARTIAL"
        else:
            status = "DEGRADED"
        return CommercialReadiness(status=status, details=details)


def update_lead_dispatch_state_from_outbox(*, lead: LeadDraft, event: OutboxEvent | None = None, result_status: str = "") -> None:
    if lead is None:
        return
    status = result_status or str(getattr(event, "status", "") or "")
    if status in {OutboxEvent.Status.PENDING, OutboxEvent.Status.PROCESSING}:
        dispatch_state = LeadDraft.DispatchStatus.PENDING
    elif status == OutboxEvent.Status.RETRY:
        dispatch_state = LeadDraft.DispatchStatus.RETRYING
    elif status == OutboxEvent.Status.SUCCEEDED:
        dispatch_state = LeadDraft.DispatchStatus.DELIVERED
    elif status == OutboxEvent.Status.SKIPPED:
        dispatch_state = LeadDraft.DispatchStatus.DRY_RUN
    elif status == OutboxEvent.Status.DEAD_LETTER:
        dispatch_state = LeadDraft.DispatchStatus.FAILED
    else:
        dispatch_state = lead.dispatch_status
    if lead.dispatch_status != dispatch_state:
        lead.dispatch_status = dispatch_state
        lead.save(update_fields=["dispatch_status", "updated_at"])


def _table_available(model) -> bool:
    try:
        return model._meta.db_table in connection.introspection.table_names()
    except Exception:
        return False


def _append_once(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _extract_labeled_value(message: str, label: str) -> str:
    pattern = rf"(?:{re.escape(label)})\s*(?:e|é|eh|:|-)?\s+([^,.;\n]+)"
    match = re.search(pattern, str(message or ""), flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _extract_after_keyword(message: str, keyword: str) -> str:
    normalized_keyword = re.escape(keyword)
    match = re.search(rf"\b{normalized_keyword}\b\s+(?:é|eh|de|para)?\s*([^,.;\n]+)", str(message or ""), flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""
