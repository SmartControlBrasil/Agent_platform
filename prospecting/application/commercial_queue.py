from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

from django.db.models import Exists, OuterRef, Q, Subquery
from django.utils import timezone

from assistant_core.consultative_policy import COMMERCIAL_INTENT_DETECTED_AT_KEY, INTENT_TYPE_KEY
from assistant_core.dialogue_memory import COMMERCIAL_INTENT_KEY
from assistant_core.qualification import is_valid_email, is_valid_phone
from conversations.models import HandoffRequest
from leads.models import LeadDraft
from prospecting.application.follow_up_queue import (
    SITUATION_OVERDUE,
    SITUATION_TODAY,
    _local_day_bounds,
    classify_follow_up_temporal,
)
from prospecting.models import (
    Prospect,
    ProspectContact,
    ProspectContactOutcome,
    ProspectFollowUp,
    ProspectOutreachDraft,
    ProspectOutreachSend,
)


class CommercialQueueCategory:
    INBOUND_HANDOFF = "INBOUND_HANDOFF"
    FOLLOW_UP_OVERDUE = "FOLLOW_UP_OVERDUE"
    FOLLOW_UP_TODAY = "FOLLOW_UP_TODAY"
    OUTREACH_PENDING = "OUTREACH_PENDING"
    WAITING_OUTCOME = "WAITING_OUTCOME"
    MISSING_CONTACT = "MISSING_CONTACT"
    READY_FOR_OUTREACH = "READY_FOR_OUTREACH"

    ALL = (
        INBOUND_HANDOFF,
        FOLLOW_UP_OVERDUE,
        FOLLOW_UP_TODAY,
        OUTREACH_PENDING,
        WAITING_OUTCOME,
        MISSING_CONTACT,
        READY_FOR_OUTREACH,
    )


SOURCE_OUTBOUND_PROSPECT = "outbound_prospect"
SOURCE_INBOUND_LIVIA = "inbound_livia"

INBOUND_KIND_ATTENDANT = "attendant"
INBOUND_KIND_QUOTE = "quote"
INBOUND_KIND_QUALIFIED = "qualified"

INBOUND_KIND_LABELS = {
    INBOUND_KIND_ATTENDANT: "Solicitação de atendente",
    INBOUND_KIND_QUOTE: "Orçamento",
    INBOUND_KIND_QUALIFIED: "Lead qualificado",
}

INBOUND_KIND_RANK = {
    INBOUND_KIND_ATTENDANT: 0,
    INBOUND_KIND_QUOTE: 1,
    INBOUND_KIND_QUALIFIED: 2,
}

QUOTE_INTENT_TYPES = frozenset(
    {
        "budget",
        "quote",
        "quote_request",
        "proposta",
        "hire",
    }
)


CATEGORY_PRECEDENCE = {category: index for index, category in enumerate(CommercialQueueCategory.ALL)}

_PRIORITY_RANK = {
    Prospect.Priority.HIGH: 0,
    Prospect.Priority.MEDIUM: 1,
    Prospect.Priority.LOW: 2,
    Prospect.Priority.UNSET: 3,
}

_FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59, tzinfo=dt_timezone.utc)

_INBOUND_CLOSED_COMMERCIAL = {
    LeadDraft.CommercialStatus.WON,
    LeadDraft.CommercialStatus.LOST,
    LeadDraft.CommercialStatus.CLOSED,
}

_INBOUND_QUALIFIED_COMMERCIAL = {
    LeadDraft.CommercialStatus.CONTACT_PENDING,
    LeadDraft.CommercialStatus.QUALIFIED,
}

_PENDING_EXPLICIT_HANDOFF_STATUSES = {
    HandoffRequest.Status.PENDING,
    HandoffRequest.Status.SENT,
}

_CLOSED_HANDOFF_STATES = {
    HandoffRequest.HandoffState.COMPLETED,
    HandoffRequest.HandoffState.CANCELLED,
}


@dataclass(frozen=True)
class CommercialQueueItem:
    category: str
    tenant_id: uuid.UUID
    prospect_id: uuid.UUID | None
    display_name: str
    priority: str
    sort_timestamp: datetime | None
    follow_up_id: uuid.UUID | None = None
    source: str = SOURCE_OUTBOUND_PROSPECT
    lead_draft_id: int | None = None
    handoff_id: int | None = None
    inbound_kind: str = ""
    need_summary: str = ""
    company: str = ""
    phone: str = ""
    email: str = ""
    origin_label: str = ""


@dataclass(frozen=True)
class CommercialQueueCounters:
    follow_up_overdue: int
    follow_up_today: int
    outreach_pending: int
    waiting_outcome: int
    missing_contact: int
    ready_for_outreach: int
    inbound_handoff: int = 0

    @property
    def total(self) -> int:
        return (
            self.follow_up_overdue
            + self.follow_up_today
            + self.outreach_pending
            + self.waiting_outcome
            + self.missing_contact
            + self.ready_for_outreach
            + self.inbound_handoff
        )


def usable_outreach_contact_filter() -> Q:
    return Q(normalized_email__gt="") | Q(normalized_phone__gt="")


def filter_usable_outreach_contacts(queryset):
    return queryset.filter(usable_outreach_contact_filter())


def prospect_has_usable_outreach_contact_exists(*, prospect_field="pk", tenant_field="tenant_id"):
    contacts = ProspectContact.objects.filter(
        prospect_id=OuterRef(prospect_field),
        tenant_id=OuterRef(tenant_field),
    ).filter(usable_outreach_contact_filter())
    return Exists(contacts)


def _apply_tenant_scope(queryset, *, tenant=None, tenant_ids=None):
    if tenant is not None:
        return queryset.filter(tenant=tenant)
    if tenant_ids is not None:
        return queryset.filter(tenant_id__in=tenant_ids)
    return queryset


def _qualified_prospects_queryset(*, tenant=None, tenant_ids=None):
    queryset = Prospect.objects.filter(qualification_status=Prospect.QualificationStatus.QUALIFIED).select_related(
        "tenant"
    )
    return _apply_tenant_scope(queryset, tenant=tenant, tenant_ids=tenant_ids)


def _annotate_prospect_pipeline_flags(queryset):
    ready_without_sent = ~Exists(
        ProspectOutreachSend.objects.filter(
            draft_id=OuterRef("pk"),
            status=ProspectOutreachSend.Status.SENT,
        )
    )
    draft_pending = ProspectOutreachDraft.objects.filter(
        prospect_id=OuterRef("pk"),
        tenant_id=OuterRef("tenant_id"),
    ).filter(
        Q(status=ProspectOutreachDraft.Status.DRAFT)
        | (Q(status=ProspectOutreachDraft.Status.READY) & ready_without_sent)
    )
    send_pipeline = ProspectOutreachSend.objects.filter(
        prospect_id=OuterRef("pk"),
        tenant_id=OuterRef("tenant_id"),
        status__in=[
            ProspectOutreachSend.Status.PENDING,
            ProspectOutreachSend.Status.SENDING,
            ProspectOutreachSend.Status.FAILED,
        ],
    )
    sent_without_outcome = ProspectOutreachSend.objects.filter(
        prospect_id=OuterRef("pk"),
        tenant_id=OuterRef("tenant_id"),
        status=ProspectOutreachSend.Status.SENT,
    ).filter(
        ~Exists(
            ProspectContactOutcome.objects.filter(
                outreach_send_id=OuterRef("pk"),
            )
        )
    )
    draft_touch = (
        ProspectOutreachDraft.objects.filter(
            prospect_id=OuterRef("pk"),
            tenant_id=OuterRef("tenant_id"),
        )
        .filter(
            Q(status=ProspectOutreachDraft.Status.DRAFT)
            | (Q(status=ProspectOutreachDraft.Status.READY) & ready_without_sent)
        )
        .order_by("-updated_at")
        .values("updated_at")[:1]
    )
    send_touch = (
        ProspectOutreachSend.objects.filter(
            prospect_id=OuterRef("pk"),
            tenant_id=OuterRef("tenant_id"),
            status__in=[
                ProspectOutreachSend.Status.PENDING,
                ProspectOutreachSend.Status.SENDING,
                ProspectOutreachSend.Status.FAILED,
            ],
        )
        .order_by("-updated_at")
        .values("updated_at")[:1]
    )
    waiting_sent_at = (
        ProspectOutreachSend.objects.filter(
            prospect_id=OuterRef("pk"),
            tenant_id=OuterRef("tenant_id"),
            status=ProspectOutreachSend.Status.SENT,
        )
        .filter(
            ~Exists(
                ProspectContactOutcome.objects.filter(
                    outreach_send_id=OuterRef("pk"),
                )
            )
        )
        .order_by("sent_at")
        .values("sent_at")[:1]
    )
    return queryset.annotate(
        has_usable_contact=prospect_has_usable_outreach_contact_exists(),
        has_draft_pending=Exists(draft_pending),
        has_send_pipeline=Exists(send_pipeline),
        has_waiting_outcome=Exists(sent_without_outcome),
        outreach_pending_draft_ts=Subquery(draft_touch),
        outreach_pending_send_ts=Subquery(send_touch),
        waiting_outcome_sent_at=Subquery(waiting_sent_at),
    )


def _classify_prospect_level(*, prospect) -> str:
    if prospect.has_draft_pending or prospect.has_send_pipeline:
        return CommercialQueueCategory.OUTREACH_PENDING
    if prospect.has_waiting_outcome:
        return CommercialQueueCategory.WAITING_OUTCOME
    if not prospect.has_usable_contact:
        return CommercialQueueCategory.MISSING_CONTACT
    return CommercialQueueCategory.READY_FOR_OUTREACH


def _prospect_level_sort_timestamp(*, prospect, category: str) -> datetime | None:
    if category == CommercialQueueCategory.OUTREACH_PENDING:
        candidates = [prospect.outreach_pending_draft_ts, prospect.outreach_pending_send_ts]
        timestamps = [value for value in candidates if value is not None]
        return max(timestamps) if timestamps else prospect.updated_at
    if category == CommercialQueueCategory.WAITING_OUTCOME:
        return prospect.waiting_outcome_sent_at or prospect.updated_at
    if category in {CommercialQueueCategory.MISSING_CONTACT, CommercialQueueCategory.READY_FOR_OUTREACH}:
        return prospect.qualified_at or prospect.updated_at
    return prospect.updated_at


def build_follow_up_commercial_items(*, tenant=None, tenant_ids=None, now=None) -> list[CommercialQueueItem]:
    now, start, end, _local_date = _local_day_bounds(now=now)
    queryset = ProspectFollowUp.objects.filter(status=ProspectFollowUp.Status.PENDING, due_at__isnull=False).filter(
        Q(due_at__lt=now) | Q(due_at__gte=start, due_at__lt=end)
    )
    queryset = _apply_tenant_scope(queryset, tenant=tenant, tenant_ids=tenant_ids).select_related("prospect", "tenant")
    items: list[CommercialQueueItem] = []
    for follow_up in queryset:
        temporal = classify_follow_up_temporal(follow_up, now=now)
        if temporal == SITUATION_OVERDUE:
            category = CommercialQueueCategory.FOLLOW_UP_OVERDUE
        elif temporal == SITUATION_TODAY:
            category = CommercialQueueCategory.FOLLOW_UP_TODAY
        else:
            continue
        items.append(
            CommercialQueueItem(
                category=category,
                tenant_id=follow_up.tenant_id,
                prospect_id=follow_up.prospect_id,
                display_name=follow_up.prospect.display_name,
                priority=follow_up.prospect.priority,
                sort_timestamp=follow_up.due_at,
                follow_up_id=follow_up.id,
            )
        )
    return items


def build_prospect_level_commercial_items(
    *,
    tenant=None,
    tenant_ids=None,
    suppress_prospect_ids: set[uuid.UUID] | None = None,
) -> list[CommercialQueueItem]:
    suppress_prospect_ids = suppress_prospect_ids or set()
    queryset = _annotate_prospect_pipeline_flags(_qualified_prospects_queryset(tenant=tenant, tenant_ids=tenant_ids))
    items: list[CommercialQueueItem] = []
    for prospect in queryset:
        if prospect.id in suppress_prospect_ids:
            continue
        category = _classify_prospect_level(prospect=prospect)
        items.append(
            CommercialQueueItem(
                category=category,
                tenant_id=prospect.tenant_id,
                prospect_id=prospect.id,
                display_name=prospect.display_name,
                priority=prospect.priority,
                sort_timestamp=_prospect_level_sort_timestamp(prospect=prospect, category=category),
            )
        )
    return items


def sort_commercial_queue_items(items: list[CommercialQueueItem]) -> list[CommercialQueueItem]:
    def sort_key(item: CommercialQueueItem):
        ts = item.sort_timestamp or _FAR_FUTURE
        inbound_rank = INBOUND_KIND_RANK.get(item.inbound_kind, 99) if item.source == SOURCE_INBOUND_LIVIA else 0
        return (
            CATEGORY_PRECEDENCE[item.category],
            inbound_rank,
            ts,
            _PRIORITY_RANK.get(item.priority, 3),
            item.display_name.casefold(),
            str(item.follow_up_id or item.prospect_id or item.lead_draft_id or item.handoff_id or ""),
        )

    return sorted(items, key=sort_key)


def _usable_contact_pair(phone: str = "", email: str = "") -> tuple[str, str]:
    phone = str(phone or "").strip()
    email = str(email or "").strip()
    usable_phone = phone if phone and is_valid_phone(phone) else ""
    usable_email = email if email and is_valid_email(email) else ""
    return usable_phone, usable_email


def _has_usable_contact_pair(phone: str = "", email: str = "") -> bool:
    usable_phone, usable_email = _usable_contact_pair(phone, email)
    return bool(usable_phone or usable_email)


def _parse_iso_datetime(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _pending_explicit_handoff_exists(*, lead_field="pk", conversation_field="conversation_id", tenant_field="tenant_id"):
    return Exists(
        HandoffRequest.objects.filter(
            tenant_id=OuterRef(tenant_field),
            first_human_action_at__isnull=True,
            reason=HandoffRequest.Reason.EXPLICIT_REQUEST,
            status__in=_PENDING_EXPLICIT_HANDOFF_STATUSES,
        )
        .exclude(handoff_state__in=_CLOSED_HANDOFF_STATES)
        .filter(Q(lead_draft_id=OuterRef(lead_field)) | Q(conversation_id=OuterRef(conversation_field)))
    )


def _actioned_or_closed_cycle_lead_exists():
    return Exists(
        LeadDraft.objects.filter(tenant_id=OuterRef("tenant_id"))
        .filter(Q(pk=OuterRef("lead_draft_id")) | Q(conversation_id=OuterRef("conversation_id")))
        .filter(
            Q(first_human_action_at__isnull=False)
            | Q(commercial_status__in=_INBOUND_CLOSED_COMMERCIAL)
            | Q(qualification_status=LeadDraft.QualificationStatus.DISQUALIFIED)
        )
    )


def _inbound_lead_candidates(*, tenant=None, tenant_ids=None):
    queryset = (
        LeadDraft.objects.filter(first_human_action_at__isnull=True)
        .exclude(commercial_status__in=_INBOUND_CLOSED_COMMERCIAL)
        .exclude(qualification_status=LeadDraft.QualificationStatus.DISQUALIFIED)
        .filter(Q(phone__gt="") | Q(email__gt="") | _pending_explicit_handoff_exists())
        .select_related("tenant", "conversation")
    )
    return _apply_tenant_scope(queryset, tenant=tenant, tenant_ids=tenant_ids)


def _inbound_handoff_candidates(*, tenant=None, tenant_ids=None):
    queryset = (
        HandoffRequest.objects.filter(
            first_human_action_at__isnull=True,
            reason=HandoffRequest.Reason.EXPLICIT_REQUEST,
            status__in=_PENDING_EXPLICIT_HANDOFF_STATUSES,
        )
        .exclude(handoff_state__in=_CLOSED_HANDOFF_STATES)
        .exclude(_actioned_or_closed_cycle_lead_exists())
        .select_related("tenant", "conversation", "lead_draft")
    )
    return _apply_tenant_scope(queryset, tenant=tenant, tenant_ids=tenant_ids)


def _handoffs_for_lead(lead: LeadDraft, handoffs_by_lead, handoffs_by_conversation) -> list:
    found = []
    seen = set()
    for item in handoffs_by_lead.get(lead.pk, ()) + handoffs_by_conversation.get(lead.conversation_id, ()):
        if item.pk in seen:
            continue
        seen.add(item.pk)
        found.append(item)
    found.sort(key=lambda item: (item.created_at, item.pk))
    return found


def _cycle_usable_contact(*, lead=None, handoffs=()) -> tuple[str, str]:
    phone = getattr(lead, "phone", "") if lead is not None else ""
    email = getattr(lead, "email", "") if lead is not None else ""
    usable_phone, usable_email = _usable_contact_pair(phone, email)
    if usable_phone or usable_email:
        return usable_phone, usable_email
    for handoff in handoffs:
        usable_phone, usable_email = _usable_contact_pair(handoff.visitor_phone, handoff.visitor_email)
        if usable_phone or usable_email:
            return usable_phone, usable_email
    return "", ""


def _lead_has_commercial_intent(lead: LeadDraft) -> bool:
    data = dict(getattr(lead, "qualification_data", None) or {})
    return bool(data.get(COMMERCIAL_INTENT_KEY))


def _lead_intent_type(lead: LeadDraft) -> str:
    data = dict(getattr(lead, "qualification_data", None) or {})
    return str(data.get(INTENT_TYPE_KEY) or "").strip().lower()


def _is_quote_intent(intent_type: str) -> bool:
    normalized = (intent_type or "").strip().lower()
    if normalized in QUOTE_INTENT_TYPES:
        return True
    return any(token in normalized for token in ("budget", "quote", "proposta", "cotacao", "cotação", "orcamento", "orçamento"))


def _lead_is_commercially_qualified(lead: LeadDraft) -> bool:
    return (
        lead.qualification_status == LeadDraft.QualificationStatus.QUALIFIED
        or lead.commercial_status in _INBOUND_QUALIFIED_COMMERCIAL
        or lead.status == LeadDraft.Status.QUALIFIED
    )


def _lead_is_actionable_inbound(*, lead: LeadDraft, handoffs, usable_contact: bool) -> bool:
    if not usable_contact:
        return False
    if handoffs:
        return True
    if not _lead_has_commercial_intent(lead):
        return False
    intent_type = _lead_intent_type(lead)
    if _is_quote_intent(intent_type):
        return True
    if _lead_is_commercially_qualified(lead):
        return True
    return False


def _inbound_kind(*, lead=None, handoffs=()) -> str:
    if handoffs:
        return INBOUND_KIND_ATTENDANT
    if lead is not None and _is_quote_intent(_lead_intent_type(lead)):
        return INBOUND_KIND_QUOTE
    return INBOUND_KIND_QUALIFIED


def _inbound_priority(kind: str) -> str:
    if kind in {INBOUND_KIND_ATTENDANT, INBOUND_KIND_QUOTE}:
        return Prospect.Priority.HIGH
    return Prospect.Priority.MEDIUM


def _inbound_sort_timestamp(*, lead=None, handoffs=()) -> datetime | None:
    candidates = []
    if lead is not None:
        detected = _parse_iso_datetime((lead.qualification_data or {}).get(COMMERCIAL_INTENT_DETECTED_AT_KEY))
        candidates.append(detected or lead.created_at)
    for handoff in handoffs:
        candidates.append(handoff.created_at)
    timestamps = [value for value in candidates if value is not None]
    return min(timestamps) if timestamps else None


def _inbound_display_name(*, lead=None, handoff=None) -> str:
    if lead is not None:
        return lead.name or lead.company or lead.email or lead.phone or "Lead inbound"
    if handoff is not None:
        return handoff.visitor_name or handoff.visitor_company or handoff.visitor_email or handoff.visitor_phone or "Atendimento inbound"
    return "Lead inbound"


def _build_inbound_item(*, lead=None, handoffs=(), handoff=None) -> CommercialQueueItem:
    related_handoffs = list(handoffs)
    if handoff is not None and all(item.pk != handoff.pk for item in related_handoffs):
        related_handoffs.append(handoff)
    related_handoffs.sort(key=lambda item: (item.created_at, item.pk))
    primary_handoff = related_handoffs[0] if related_handoffs else handoff
    kind = _inbound_kind(lead=lead, handoffs=related_handoffs)
    phone, email = _cycle_usable_contact(lead=lead, handoffs=related_handoffs)
    tenant_id = lead.tenant_id if lead is not None else primary_handoff.tenant_id
    return CommercialQueueItem(
        category=CommercialQueueCategory.INBOUND_HANDOFF,
        tenant_id=tenant_id,
        prospect_id=None,
        display_name=_inbound_display_name(lead=lead, handoff=primary_handoff),
        priority=_inbound_priority(kind),
        sort_timestamp=_inbound_sort_timestamp(lead=lead, handoffs=related_handoffs),
        source=SOURCE_INBOUND_LIVIA,
        lead_draft_id=lead.pk if lead is not None else getattr(primary_handoff, "lead_draft_id", None),
        handoff_id=primary_handoff.pk if primary_handoff is not None else None,
        inbound_kind=kind,
        need_summary=(getattr(lead, "need_summary", "") or getattr(primary_handoff, "summary", "") or "").strip(),
        company=(getattr(lead, "company", "") or getattr(primary_handoff, "visitor_company", "") or "").strip(),
        phone=phone,
        email=email,
        origin_label="Lívia",
    )


def build_inbound_livia_commercial_items(*, tenant=None, tenant_ids=None) -> list[CommercialQueueItem]:
    leads = list(_inbound_lead_candidates(tenant=tenant, tenant_ids=tenant_ids))
    handoffs = list(_inbound_handoff_candidates(tenant=tenant, tenant_ids=tenant_ids))
    handoffs_by_lead: dict = {}
    handoffs_by_conversation: dict = {}
    for item in handoffs:
        if item.lead_draft_id:
            handoffs_by_lead.setdefault(item.lead_draft_id, []).append(item)
        if item.conversation_id:
            handoffs_by_conversation.setdefault(item.conversation_id, []).append(item)

    items: list[CommercialQueueItem] = []
    claimed_lead_ids: set[int] = set()
    claimed_conversation_ids: set[int] = set()
    claimed_handoff_ids: set[int] = set()

    for lead in leads:
        related = _handoffs_for_lead(lead, handoffs_by_lead, handoffs_by_conversation)
        phone, email = _cycle_usable_contact(lead=lead, handoffs=related)
        if not _lead_is_actionable_inbound(lead=lead, handoffs=related, usable_contact=bool(phone or email)):
            continue
        items.append(_build_inbound_item(lead=lead, handoffs=related))
        claimed_lead_ids.add(lead.pk)
        if lead.conversation_id:
            claimed_conversation_ids.add(lead.conversation_id)
        claimed_handoff_ids.update(item.pk for item in related)

    for handoff in handoffs:
        if handoff.pk in claimed_handoff_ids:
            continue
        if handoff.lead_draft_id and handoff.lead_draft_id in claimed_lead_ids:
            continue
        if handoff.conversation_id and handoff.conversation_id in claimed_conversation_ids:
            continue
        related_lead = handoff.lead_draft
        if related_lead is not None and (
            related_lead.first_human_action_at
            or related_lead.commercial_status in _INBOUND_CLOSED_COMMERCIAL
            or related_lead.qualification_status == LeadDraft.QualificationStatus.DISQUALIFIED
        ):
            continue
        phone, email = _cycle_usable_contact(lead=related_lead, handoffs=[handoff])
        if not (phone or email):
            continue
        items.append(_build_inbound_item(lead=related_lead, handoffs=[handoff], handoff=handoff))
        claimed_handoff_ids.add(handoff.pk)
        if handoff.conversation_id:
            claimed_conversation_ids.add(handoff.conversation_id)
        if handoff.lead_draft_id:
            claimed_lead_ids.add(handoff.lead_draft_id)
    return items


def build_commercial_queue(
    *,
    tenant=None,
    tenant_ids=None,
    now=None,
    compact: bool = True,
) -> list[CommercialQueueItem]:
    follow_up_items = build_follow_up_commercial_items(tenant=tenant, tenant_ids=tenant_ids, now=now)
    suppress_ids: set[uuid.UUID] = set()
    if compact:
        for item in follow_up_items:
            if item.prospect_id and item.category in {
                CommercialQueueCategory.FOLLOW_UP_OVERDUE,
                CommercialQueueCategory.FOLLOW_UP_TODAY,
            }:
                suppress_ids.add(item.prospect_id)
    prospect_items = build_prospect_level_commercial_items(
        tenant=tenant,
        tenant_ids=tenant_ids,
        suppress_prospect_ids=suppress_ids,
    )
    inbound_items = build_inbound_livia_commercial_items(tenant=tenant, tenant_ids=tenant_ids)
    return sort_commercial_queue_items([*inbound_items, *follow_up_items, *prospect_items])


def commercial_queue_counters_from_items(items) -> CommercialQueueCounters:
    counts = {category: 0 for category in CommercialQueueCategory.ALL}
    for item in items:
        counts[item.category] += 1
    return CommercialQueueCounters(
        follow_up_overdue=counts[CommercialQueueCategory.FOLLOW_UP_OVERDUE],
        follow_up_today=counts[CommercialQueueCategory.FOLLOW_UP_TODAY],
        outreach_pending=counts[CommercialQueueCategory.OUTREACH_PENDING],
        waiting_outcome=counts[CommercialQueueCategory.WAITING_OUTCOME],
        missing_contact=counts[CommercialQueueCategory.MISSING_CONTACT],
        ready_for_outreach=counts[CommercialQueueCategory.READY_FOR_OUTREACH],
        inbound_handoff=counts[CommercialQueueCategory.INBOUND_HANDOFF],
    )


def compute_commercial_queue_action_now(counters: CommercialQueueCounters) -> int:
    return (
        counters.follow_up_overdue
        + counters.follow_up_today
        + counters.outreach_pending
        + counters.inbound_handoff
    )


def compute_commercial_queue_counters(*, tenant=None, tenant_ids=None, now=None) -> CommercialQueueCounters:
    items = build_commercial_queue(tenant=tenant, tenant_ids=tenant_ids, now=now, compact=True)
    return commercial_queue_counters_from_items(items)
