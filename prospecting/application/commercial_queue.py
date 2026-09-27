from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

from django.db.models import Exists, OuterRef, Q, Subquery
from django.utils import timezone

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
    FOLLOW_UP_OVERDUE = "FOLLOW_UP_OVERDUE"
    FOLLOW_UP_TODAY = "FOLLOW_UP_TODAY"
    OUTREACH_PENDING = "OUTREACH_PENDING"
    WAITING_OUTCOME = "WAITING_OUTCOME"
    MISSING_CONTACT = "MISSING_CONTACT"
    READY_FOR_OUTREACH = "READY_FOR_OUTREACH"

    ALL = (
        FOLLOW_UP_OVERDUE,
        FOLLOW_UP_TODAY,
        OUTREACH_PENDING,
        WAITING_OUTCOME,
        MISSING_CONTACT,
        READY_FOR_OUTREACH,
    )


CATEGORY_PRECEDENCE = {category: index for index, category in enumerate(CommercialQueueCategory.ALL)}

_PRIORITY_RANK = {
    Prospect.Priority.HIGH: 0,
    Prospect.Priority.MEDIUM: 1,
    Prospect.Priority.LOW: 2,
    Prospect.Priority.UNSET: 3,
}

_FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59, tzinfo=dt_timezone.utc)


@dataclass(frozen=True)
class CommercialQueueItem:
    category: str
    tenant_id: uuid.UUID
    prospect_id: uuid.UUID
    display_name: str
    priority: str
    sort_timestamp: datetime | None
    follow_up_id: uuid.UUID | None = None


@dataclass(frozen=True)
class CommercialQueueCounters:
    follow_up_overdue: int
    follow_up_today: int
    outreach_pending: int
    waiting_outcome: int
    missing_contact: int
    ready_for_outreach: int

    @property
    def total(self) -> int:
        return (
            self.follow_up_overdue
            + self.follow_up_today
            + self.outreach_pending
            + self.waiting_outcome
            + self.missing_contact
            + self.ready_for_outreach
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
        return (
            CATEGORY_PRECEDENCE[item.category],
            ts,
            _PRIORITY_RANK.get(item.priority, 3),
            item.display_name.casefold(),
            str(item.follow_up_id or item.prospect_id),
        )

    return sorted(items, key=sort_key)


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
            if item.category in {
                CommercialQueueCategory.FOLLOW_UP_OVERDUE,
                CommercialQueueCategory.FOLLOW_UP_TODAY,
            }:
                suppress_ids.add(item.prospect_id)
    prospect_items = build_prospect_level_commercial_items(
        tenant=tenant,
        tenant_ids=tenant_ids,
        suppress_prospect_ids=suppress_ids,
    )
    return sort_commercial_queue_items([*follow_up_items, *prospect_items])


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
    )


def compute_commercial_queue_action_now(counters: CommercialQueueCounters) -> int:
    return counters.follow_up_overdue + counters.follow_up_today + counters.outreach_pending


def compute_commercial_queue_counters(*, tenant=None, tenant_ids=None, now=None) -> CommercialQueueCounters:
    items = build_commercial_queue(tenant=tenant, tenant_ids=tenant_ids, now=now, compact=True)
    return commercial_queue_counters_from_items(items)
