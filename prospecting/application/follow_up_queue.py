from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.db.models import Case, Count, IntegerField, OuterRef, Q, Subquery, Value, When
from django.utils import timezone

from prospecting.models import Prospect, ProspectContactOutcome, ProspectFollowUp


SITUATION_PENDING = "pending"
SITUATION_OVERDUE = "overdue"
SITUATION_TODAY = "today"
SITUATION_UPCOMING = "upcoming"
SITUATION_COMPLETED = "completed"
SITUATION_CANCELLED = "cancelled"
SITUATION_ALL = "all"

PERIOD_TODAY = "today"
PERIOD_NEXT_7 = "next_7"
PERIOD_NEXT_30 = "next_30"


@dataclass(frozen=True)
class FollowUpQueueCounters:
    overdue: int
    today: int
    next_7_days: int
    pending_total: int


def _local_day_bounds(*, now=None):
    now = now or timezone.now()
    local_date = timezone.localdate(now)
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(local_date, time.min), tz)
    end = start + timedelta(days=1)
    return now, start, end, local_date


def classify_follow_up_temporal(follow_up, *, now=None) -> str | None:
    if follow_up.status != ProspectFollowUp.Status.PENDING:
        return None
    now, start, end, _local_date = _local_day_bounds(now=now)
    if follow_up.due_at is None:
        return SITUATION_UPCOMING
    due = follow_up.due_at
    if due < now:
        return SITUATION_OVERDUE
    if start <= due < end:
        return SITUATION_TODAY
    return SITUATION_UPCOMING


def base_follow_up_queue_queryset(*, tenant=None, tenant_ids=None):
    queryset = ProspectFollowUp.objects.select_related(
        "prospect",
        "tenant",
        "contact",
        "created_by",
        "completed_by",
    )
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    elif tenant_ids is not None:
        queryset = queryset.filter(tenant_id__in=tenant_ids)
    latest_outcome = ProspectContactOutcome.objects.filter(prospect_id=OuterRef("prospect_id")).order_by(
        "-occurred_at", "-created_at"
    )
    queryset = queryset.annotate(latest_outcome=Subquery(latest_outcome.values("outcome")[:1]))
    return queryset


def annotate_follow_up_sort_bucket(queryset, *, now=None):
    now, start, end, _local_date = _local_day_bounds(now=now)
    return queryset.annotate(
        sort_bucket=Case(
            When(
                status=ProspectFollowUp.Status.PENDING,
                due_at__isnull=False,
                due_at__lt=now,
                then=Value(0),
            ),
            When(
                status=ProspectFollowUp.Status.PENDING,
                due_at__gte=start,
                due_at__lt=end,
                then=Value(1),
            ),
            When(status=ProspectFollowUp.Status.PENDING, then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        )
    )


def apply_follow_up_queue_filters(
    queryset,
    *,
    situation=SITUATION_PENDING,
    action_type="",
    prospect_query="",
    contact_query="",
    priority="",
    period="",
    now=None,
):
    now, start, end, local_date = _local_day_bounds(now=now)
    situation = (situation or SITUATION_PENDING).strip().lower()
    if situation == SITUATION_PENDING:
        queryset = queryset.filter(status=ProspectFollowUp.Status.PENDING)
    elif situation == SITUATION_OVERDUE:
        queryset = queryset.filter(status=ProspectFollowUp.Status.PENDING, due_at__lt=now)
    elif situation == SITUATION_TODAY:
        queryset = queryset.filter(status=ProspectFollowUp.Status.PENDING, due_at__gte=start, due_at__lt=end)
    elif situation == SITUATION_UPCOMING:
        queryset = queryset.filter(status=ProspectFollowUp.Status.PENDING).filter(Q(due_at__gte=end) | Q(due_at__isnull=True))
    elif situation == SITUATION_COMPLETED:
        queryset = queryset.filter(status=ProspectFollowUp.Status.COMPLETED)
    elif situation == SITUATION_CANCELLED:
        queryset = queryset.filter(status=ProspectFollowUp.Status.CANCELLED)
    elif situation != SITUATION_ALL:
        queryset = queryset.filter(status=ProspectFollowUp.Status.PENDING)

    action_type = (action_type or "").strip().upper()
    if action_type and action_type in ProspectFollowUp.ActionType.values:
        queryset = queryset.filter(action_type=action_type)

    prospect_query = (prospect_query or "").strip()
    if prospect_query:
        queryset = queryset.filter(prospect__display_name__icontains=prospect_query)

    contact_query = (contact_query or "").strip()
    if contact_query:
        queryset = queryset.filter(
            Q(contact__name__icontains=contact_query)
            | Q(contact__email__icontains=contact_query)
            | Q(contact__phone__icontains=contact_query)
        )

    priority = (priority or "").strip().upper()
    if priority == "UNSET":
        queryset = queryset.filter(prospect__priority=Prospect.Priority.UNSET)
    elif priority in {Prospect.Priority.HIGH, Prospect.Priority.MEDIUM, Prospect.Priority.LOW}:
        queryset = queryset.filter(prospect__priority=priority)

    period = (period or "").strip().lower()
    if period == PERIOD_TODAY:
        queryset = queryset.filter(due_at__gte=start, due_at__lt=end)
    elif period == PERIOD_NEXT_7:
        queryset = queryset.filter(due_at__gte=start, due_at__lt=start + timedelta(days=7))
    elif period == PERIOD_NEXT_30:
        queryset = queryset.filter(due_at__gte=start, due_at__lt=start + timedelta(days=30))

    return queryset


def order_follow_up_queue(queryset):
    return queryset.order_by("sort_bucket", "due_at", "created_at")


def compute_follow_up_queue_counters(*, tenant=None, tenant_ids=None, now=None) -> FollowUpQueueCounters:
    now, start, end, _local_date = _local_day_bounds(now=now)
    pending = base_follow_up_queue_queryset(tenant=tenant, tenant_ids=tenant_ids).filter(
        status=ProspectFollowUp.Status.PENDING
    )
    return FollowUpQueueCounters(
        overdue=pending.filter(due_at__isnull=False, due_at__lt=now).count(),
        today=pending.filter(due_at__gte=start, due_at__lt=end).count(),
        next_7_days=pending.filter(due_at__gte=start, due_at__lt=start + timedelta(days=7)).count(),
        pending_total=pending.count(),
    )
