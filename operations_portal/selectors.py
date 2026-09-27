from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Prefetch, Q, Value, When
from django.db.utils import OperationalError, ProgrammingError
from django.shortcuts import get_object_or_404

from conversations.models import Conversation, HandoffRequest, Message
from integrations.models import OutboxEvent, TenantWebhookConfig
from leads.models import LeadDraft
from leads.services.commercial import CommercialReadinessService
from tenants.models import Tenant

from .operational_readiness import TenantOperationalReadinessService

from .formatters import (
    can_retry_crm_dispatch,
    compact_external_id,
    contact_summary,
    handoff_contact_summary,
    handoff_priority_label,
    handoff_priority_tone,
    handoff_reason_label,
    handoff_status_label,
    handoff_status_tone,
    lead_crm_state,
    lead_dispatch_status_label,
    lead_dispatch_status_tone,
    lead_handoff_status_label,
    lead_status_label,
    lead_status_tone,
    qualification_status_label,
    qualification_status_tone,
    mask_email,
    mask_phone,
    short_text,
)

CONVERSATION_LEAD_STATE_LABELS = {
    Conversation.LeadState.DISCOVERY: "Descoberta",
    Conversation.LeadState.OFFER_HANDOFF: "Oferta de atendimento humano",
    Conversation.LeadState.COLLECT_NEED: "Coleta da necessidade",
    Conversation.LeadState.COLLECT_NAME_COMPANY: "Nome e empresa",
    Conversation.LeadState.COLLECT_CONTACT: "Contato",
    Conversation.LeadState.QUALIFIED: "Qualificada",
    Conversation.LeadState.CLOSED: "Encerrada",
}

HANDOFF_STATUS_LABELS = {
    HandoffRequest.Status.PENDING: "Pendente",
    HandoffRequest.Status.SENT: "Enviado",
    HandoffRequest.Status.RESOLVED: "Resolvido",
    HandoffRequest.Status.CANCELLED: "Cancelado",
}

HANDOFF_PRIORITY_LABELS = {
    HandoffRequest.Priority.LOW: "Baixa",
    HandoffRequest.Priority.NORMAL: "Normal",
    HandoffRequest.Priority.HIGH: "Alta",
    HandoffRequest.Priority.URGENT: "Urgente",
}

PAGE_SIZE = 12
OUTBOX_STATUS_LABELS = {
    OutboxEvent.Status.PENDING: "Pendente",
    OutboxEvent.Status.PROCESSING: "Processando",
    OutboxEvent.Status.SUCCEEDED: "Concluído",
    OutboxEvent.Status.RETRY: "Retry agendado",
    OutboxEvent.Status.DEAD_LETTER: "Dead letter",
    OutboxEvent.Status.SKIPPED: "Ignorado",
}
OUTBOX_STATUS_TONES = {
    OutboxEvent.Status.PENDING: "warning",
    OutboxEvent.Status.PROCESSING: "info",
    OutboxEvent.Status.SUCCEEDED: "success",
    OutboxEvent.Status.RETRY: "warning",
    OutboxEvent.Status.DEAD_LETTER: "danger",
    OutboxEvent.Status.SKIPPED: "secondary",
}
RETRYABLE_ERROR_HINTS = (
    "timeout",
    "temporar",
    "temporar",
    "unavailable",
    "connection",
    "429",
    "503",
    "504",
    "502",
    "requestsunavailableerror",
)
SENSITIVE_ERROR_HINTS = (
    "authorization",
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
)


@dataclass(frozen=True)
class IntegrationStatus:
    label: str
    state: str
    detail: str
    tone: str


def get_dashboard_context(period_value=None, *, tenant=None, user=None):
    from .analytics import get_dashboard_analytics

    analytics = get_dashboard_analytics(period_value, tenant=tenant)
    recent_conversations = list(_scope_queryset(Conversation.objects.select_related("tenant"), tenant).order_by("-updated_at")[:8])
    for conversation in recent_conversations:
        decorate_conversation(conversation)
    recent_leads = list(_scope_queryset(LeadDraft.objects.select_related("tenant", "conversation"), tenant).order_by("-updated_at")[:8])
    for lead in recent_leads:
        decorate_lead(lead)

    kpis = analytics["kpis"]
    kpis.update(
        {
            "recent_conversations": len(recent_conversations),
            "qualified_leads": kpis["period_leads_qualified"],
            "sent_to_crm": kpis["period_leads_sent"],
            "failed_leads": kpis["period_leads_failed"],
        }
    )

    return {
        "period": analytics["period"],
        "kpis": kpis,
        "dashboard_charts": analytics,
        "recent_conversations": recent_conversations,
        "recent_leads": recent_leads,
        "integration_statuses": get_integration_statuses(tenant=tenant),
        "commercial_readiness": CommercialReadinessService().readiness(tenant=tenant) if tenant is not None else None,
        "operational_status": TenantOperationalReadinessService().for_tenant(tenant) if tenant is not None else None,
        "active_tenants": _active_tenant_queryset(tenant)[:8],
        "operational_work": _operational_work_dashboard(tenant=tenant, user=user),
        "prospecting_follow_up_summary": _prospecting_follow_up_dashboard(tenant=tenant, user=user),
        "prospecting_my_queue_summary": _prospecting_my_queue_dashboard(tenant=tenant, user=user),
    }


def _prospecting_my_queue_dashboard(*, tenant, user=None):
    if tenant is None:
        return None
    from tenants.access import CAPABILITY_COMMERCIAL_VIEW, user_has_tenant_capability

    if user is not None and not user_has_tenant_capability(user, tenant, CAPABILITY_COMMERCIAL_VIEW):
        return None
    from prospecting.application.commercial_queue import (
        compute_commercial_queue_action_now,
        compute_commercial_queue_counters,
    )

    counters = compute_commercial_queue_counters(tenant=tenant)
    return {
        "action_now": compute_commercial_queue_action_now(counters),
        "overdue": counters.follow_up_overdue,
        "today": counters.follow_up_today,
        "outreach_pending": counters.outreach_pending,
    }


def _prospecting_follow_up_dashboard(*, tenant, user=None):
    if tenant is None:
        return None
    from tenants.access import CAPABILITY_COMMERCIAL_VIEW, user_has_tenant_capability

    if user is not None and not user_has_tenant_capability(user, tenant, CAPABILITY_COMMERCIAL_VIEW):
        return None
    from prospecting.application.follow_up_queue import compute_follow_up_queue_counters

    counters = compute_follow_up_queue_counters(tenant=tenant)
    return {
        "overdue": counters.overdue,
        "today": counters.today,
        "pending_total": counters.pending_total,
    }


def _operational_work_dashboard(*, tenant, user=None):
    if tenant is None:
        return None
    from knowledge_base.rag.operational_work_queue import build_personal_work_count, build_work_queue_summary
    from tenants.access import CAPABILITY_KNOWLEDGE_BASE_VIEW, get_active_membership, user_has_tenant_capability

    if user is not None and not user_has_tenant_capability(user, tenant, CAPABILITY_KNOWLEDGE_BASE_VIEW):
        return None
    summary = build_work_queue_summary(tenant=tenant)
    membership = get_active_membership(user, tenant) if user is not None else None
    summary = dict(summary)
    summary["my_count"] = build_personal_work_count(tenant=tenant, membership=membership) if membership else 0
    from knowledge_base.rag.operational_analytics import build_operational_health_summary

    try:
        summary["health"] = build_operational_health_summary(tenant=tenant)
    except Exception:
        summary["health"] = None
    return summary


def get_integration_statuses(*, tenant=None):
    return [
        get_crm_status(),
        _status(
            "OpenAI",
            bool(getattr(settings, "LIVIA_AI_ENABLED", False)),
            bool(getattr(settings, "LIVIA_AI_DRY_RUN", True)),
            "Respostas assistidas por IA",
        ),
        _status(
            "Webhooks",
            bool(getattr(settings, "LIVIA_WEBHOOKS_ENABLED", False)),
            bool(getattr(settings, "LIVIA_WEBHOOKS_DRY_RUN", True)),
            f"{_scope_queryset(TenantWebhookConfig.objects.filter(is_active=True), tenant).count()} config. ativas",
        ),
        get_notification_status(),
    ]


def get_crm_status():
    enabled = bool(getattr(settings, "SMART360_LEAD_DISPATCH_ENABLED", False))
    dry_run = bool(getattr(settings, "SMART360_LEAD_DISPATCH_DRY_RUN", True))
    has_base_url = bool(str(getattr(settings, "SMART360_BASE_URL", "") or "").strip())
    has_token = bool(str(getattr(settings, "SMART360_M2M_TOKEN", "") or "").strip())

    if not enabled:
        return IntegrationStatus(
            label="CRM Smart360",
            state="Desligado",
            detail="Dispatch de leads desabilitado",
            tone="secondary",
        )
    if dry_run:
        return IntegrationStatus(
            label="CRM Smart360",
            state="Dry-run",
            detail="Dispatch real não será chamado",
            tone="warning",
        )
    if not (has_base_url and has_token):
        return IntegrationStatus(
            label="CRM Smart360",
            state="Configuração incompleta",
            detail="Modo real sem endpoint ou credencial configurada",
            tone="danger",
        )
    return IntegrationStatus(
        label="CRM Smart360",
        state="Ativo real",
        detail="Dispatch real habilitado",
        tone="success",
    )


def _status(label, enabled, dry_run, detail):
    if not enabled:
        return IntegrationStatus(label=label, state="Desligado", detail=detail, tone="secondary")
    if dry_run:
        return IntegrationStatus(label=label, state="Dry-run", detail=detail, tone="warning")
    return IntegrationStatus(label=label, state="Ativo", detail=detail, tone="success")


def clean_querystring(querydict):
    params = querydict.copy()
    params.pop("page", None)
    return params.urlencode()


def get_conversation_list(form, *, page_number=1, tenant=None):
    queryset = (
        _scope_queryset(Conversation.objects.select_related("tenant", "lead_draft"), tenant)
        .prefetch_related(
            Prefetch(
                "handoff_requests",
                queryset=HandoffRequest.objects.select_related("lead_draft").order_by("-created_at"),
                to_attr="prefetched_handoffs",
            )
        )
        .annotate(message_count=Count("messages", distinct=True))
        .order_by("-updated_at")
    )
    queryset = _filter_conversations(queryset, form)
    page = Paginator(queryset, PAGE_SIZE).get_page(page_number)
    for conversation in page.object_list:
        decorate_conversation(conversation)
    return page


def get_conversation_detail(pk, *, tenant=None):
    conversation = get_object_or_404(
        _scope_queryset(Conversation.objects.select_related("tenant", "lead_draft"), tenant).prefetch_related(
            Prefetch("messages", queryset=Message.objects.order_by("created_at")),
            Prefetch(
                "handoff_requests",
                queryset=HandoffRequest.objects.select_related("lead_draft").order_by("-created_at"),
                to_attr="prefetched_handoffs",
            ),
        ),
        pk=pk,
    )
    decorate_conversation(conversation)
    if hasattr(conversation, "lead_draft"):
        decorate_lead(conversation.lead_draft)
    for handoff in getattr(conversation, "prefetched_handoffs", []):
        decorate_handoff(handoff)
    return conversation


def get_lead_list(form, *, page_number=1, tenant=None):
    queryset = (
        _scope_queryset(LeadDraft.objects.select_related("tenant", "conversation"), tenant)
        .prefetch_related(
            Prefetch(
                "handoff_requests",
                queryset=HandoffRequest.objects.select_related("conversation").order_by("-created_at"),
                to_attr="prefetched_handoffs",
            )
        )
        .order_by("-updated_at", "-created_at")
    )
    queryset = _filter_leads(queryset, form)
    page = Paginator(queryset, PAGE_SIZE).get_page(page_number)
    _decorate_lead_outbox(page.object_list)
    for lead in page.object_list:
        decorate_lead(lead)
    return page


def get_lead_detail(pk, *, tenant=None):
    lead = get_object_or_404(
        _scope_queryset(LeadDraft.objects.select_related("tenant", "conversation"), tenant).prefetch_related(
            Prefetch(
                "handoff_requests",
                queryset=HandoffRequest.objects.select_related("conversation").order_by("-created_at"),
                to_attr="prefetched_handoffs",
            )
        ),
        pk=pk,
    )
    _decorate_lead_outbox([lead])
    decorate_lead(lead)
    for handoff in getattr(lead, "prefetched_handoffs", []):
        decorate_handoff(handoff)
    if lead.conversation is not None:
        decorate_conversation(lead.conversation)
    return lead


def decorate_conversation(conversation):
    conversation.lead_state_label = CONVERSATION_LEAD_STATE_LABELS.get(
        conversation.lead_state, conversation.get_lead_state_display()
    )
    handoffs = list(getattr(conversation, "prefetched_handoffs", []))
    conversation.latest_handoff = handoffs[0] if handoffs else None
    if conversation.latest_handoff is not None:
        decorate_handoff(conversation.latest_handoff)
    return conversation


def decorate_lead(lead):
    lead.status_label = lead_status_label(lead.status)
    lead.status_tone = lead_status_tone(lead.status)
    lead.qualification_status_label = qualification_status_label(getattr(lead, "qualification_status", ""))
    lead.qualification_status_tone = qualification_status_tone(getattr(lead, "qualification_status", ""))
    lead.handoff_status_label = lead_handoff_status_label(getattr(lead, "handoff_status", ""))
    lead.dispatch_status_label = lead_dispatch_status_label(getattr(lead, "dispatch_status", ""))
    lead.dispatch_status_tone = lead_dispatch_status_tone(getattr(lead, "dispatch_status", ""))
    lead.crm_state = lead_crm_state(lead)
    lead.crm_external_id_compact = compact_external_id(lead.crm_external_id)
    lead.need_summary_short = short_text(lead.need_summary, limit=100)
    lead.contact = contact_summary(lead)
    lead.masked_email = mask_email(lead.email)
    lead.masked_phone = mask_phone(lead.phone)
    lead.can_retry_crm_dispatch = can_retry_crm_dispatch(lead)
    lead.crm_error_sanitized = sanitize_error_message(lead.crm_error)
    lead.collected_field_labels = sorted((lead.field_sources or {}).keys())
    lead.missing_fields = []
    try:
        from leads.services.commercial import QualificationPolicy, QualificationService
        policy = QualificationPolicy(slug=str(getattr(lead, "qualification_policy", "") or "default"))
        lead.missing_fields = QualificationService(policy=policy).missing_fields(lead, policy=policy)
    except Exception:
        lead.missing_fields = []
    handoffs = list(getattr(lead, "prefetched_handoffs", []))
    lead.latest_handoff = handoffs[0] if handoffs else None
    if lead.latest_handoff is not None:
        decorate_handoff(lead.latest_handoff)
    return lead


def decorate_handoff(handoff, *, detail=False):
    handoff.status_label = handoff_status_label(handoff.status)
    handoff.handoff_state_label = str(getattr(handoff, "handoff_state", "") or "-").replace("_", " ").title()
    handoff.dispatch_state_label = str(getattr(handoff, "dispatch_state", "") or "-").replace("_", " ").title()
    handoff.status_tone = handoff_status_tone(handoff.status)
    handoff.priority_label = handoff_priority_label(handoff.priority)
    handoff.priority_tone = handoff_priority_tone(handoff.priority)
    handoff.reason_label = handoff_reason_label(handoff.reason)
    handoff.summary_short = short_text(handoff.summary, limit=100)
    handoff.contact = handoff_contact_summary(handoff, masked=not detail)
    return handoff


def sanitize_error_message(value):
    text = short_text(value, limit=220)
    lowered = text.lower()
    if "traceback" in lowered:
        return "Detalhe técnico interno ocultado."
    if any(fragment in lowered for fragment in SENSITIVE_ERROR_HINTS):
        return "Detalhe técnico sensível ocultado."
    return text or "-"


def classify_failure_kind(*, lead, outbox_event):
    if outbox_event is not None:
        if outbox_event.status in {OutboxEvent.Status.PENDING, OutboxEvent.Status.PROCESSING, OutboxEvent.Status.RETRY}:
            return "temporaria", "Temporária"
        if outbox_event.status in {OutboxEvent.Status.DEAD_LETTER, OutboxEvent.Status.SKIPPED}:
            return "definitiva", "Definitiva"
        if outbox_event.status == OutboxEvent.Status.SUCCEEDED:
            return "resolvida", "Resolvida"
    lowered = str(getattr(lead, "crm_error", "") or "").lower()
    if lowered and any(fragment in lowered for fragment in RETRYABLE_ERROR_HINTS):
        return "temporaria", "Temporária"
    if lowered:
        return "indefinida", "Indefinida"
    return "sem_falha", "Sem falha"


def _decorate_lead_outbox(leads):
    leads = list(leads or [])
    if not leads:
        return
    lead_ids = [str(lead.pk) for lead in leads]
    tenant_ids = list({lead.tenant_id for lead in leads})
    latest_by_key = {}
    try:
        events = (
            OutboxEvent.objects.filter(
                event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
                aggregate_id__in=lead_ids,
                tenant_id__in=tenant_ids,
            )
            .order_by("aggregate_id", "-created_at")
        )
        for event in events:
            key = (event.tenant_id, event.aggregate_id)
            if key not in latest_by_key:
                latest_by_key[key] = event
    except (OperationalError, ProgrammingError):
        for lead in leads:
            _set_outbox_defaults(
                lead,
                schema_available=False,
                note="Outbox indisponível neste banco local (migration pendente).",
            )
        return

    for lead in leads:
        event = latest_by_key.get((lead.tenant_id, str(lead.pk)))
        if event is None:
            _set_outbox_defaults(lead, schema_available=True, note="Sem evento de outbox para este lead.")
            _set_failure_kind(lead, outbox_event=None)
            continue
        _set_outbox_from_event(lead, event)
        _set_failure_kind(lead, outbox_event=event)


def _set_failure_kind(lead, *, outbox_event):
    code, label = classify_failure_kind(lead=lead, outbox_event=outbox_event)
    lead.failure_kind_code = code
    lead.failure_kind_label = label


def _set_outbox_defaults(lead, *, schema_available: bool, note: str):
    lead.outbox_schema_available = schema_available
    lead.outbox_note = note
    lead.outbox_status_code = ""
    lead.outbox_status_label = "Indisponível" if not schema_available else "Sem evento"
    lead.outbox_status_tone = "secondary"
    lead.outbox_event_id = ""
    lead.outbox_event_id_compact = "-"
    lead.outbox_attempts = 0
    lead.outbox_max_attempts = 0
    lead.outbox_last_attempt_at = None
    lead.outbox_next_retry_at = None
    lead.outbox_last_error_code = ""
    lead.outbox_last_error_message = "-"
    lead.outbox_is_active = False
    lead.outbox_processed_at = None


def _set_outbox_from_event(lead, event):
    lead.outbox_schema_available = True
    lead.outbox_note = ""
    lead.outbox_status_code = event.status
    lead.outbox_status_label = OUTBOX_STATUS_LABELS.get(event.status, event.status)
    lead.outbox_status_tone = OUTBOX_STATUS_TONES.get(event.status, "secondary")
    lead.outbox_event_id = str(event.event_id)
    lead.outbox_event_id_compact = compact_external_id(str(event.event_id))
    lead.outbox_attempts = int(event.attempts or 0)
    lead.outbox_max_attempts = int(event.max_attempts or 0)
    lead.outbox_last_attempt_at = event.last_attempt_at
    lead.outbox_next_retry_at = event.available_at if event.status in {OutboxEvent.Status.PENDING, OutboxEvent.Status.RETRY} else None
    lead.outbox_last_error_code = str(event.last_error_code or "")
    lead.outbox_last_error_message = sanitize_error_message(event.last_error_message)
    lead.outbox_is_active = event.status in {OutboxEvent.Status.PENDING, OutboxEvent.Status.PROCESSING, OutboxEvent.Status.RETRY}
    lead.outbox_processed_at = event.processed_at


def _filter_conversations(queryset, form):
    if not form.is_valid():
        return queryset
    data = form.cleaned_data
    if data.get("tenant"):
        queryset = queryset.filter(tenant=data["tenant"])
    if data.get("lead_state"):
        queryset = queryset.filter(lead_state=data["lead_state"])
    if data.get("qualified") == "yes":
        queryset = queryset.filter(is_qualified=True)
    if data.get("qualified") == "no":
        queryset = queryset.filter(is_qualified=False)
    if data.get("start_date"):
        queryset = queryset.filter(updated_at__date__gte=data["start_date"])
    if data.get("end_date"):
        queryset = queryset.filter(updated_at__date__lte=data["end_date"])
    if data.get("q"):
        queryset = queryset.filter(session_id__icontains=data["q"].strip())
    return queryset


def _filter_leads(queryset, form):
    if not form.is_valid():
        return queryset
    data = form.cleaned_data
    sent_lookup = Q(status=LeadDraft.Status.SENT_TO_CRM) | Q(crm_external_id__gt="") | Q(sent_to_crm_at__isnull=False)
    failed_lookup = Q(status=LeadDraft.Status.FAILED) | Q(crm_error__gt="")
    if data.get("tenant"):
        queryset = queryset.filter(tenant=data["tenant"])
    if data.get("status"):
        queryset = queryset.filter(status=data["status"])
    if data.get("crm_sent") == "yes":
        queryset = queryset.filter(sent_lookup)
    if data.get("crm_sent") == "no":
        queryset = queryset.exclude(sent_lookup)
    if data.get("dispatch_failed") == "yes":
        queryset = queryset.filter(failed_lookup)
    if data.get("dispatch_failed") == "no":
        queryset = queryset.exclude(failed_lookup)
    if data.get("start_date"):
        queryset = queryset.filter(updated_at__date__gte=data["start_date"])
    if data.get("end_date"):
        queryset = queryset.filter(updated_at__date__lte=data["end_date"])
    if data.get("q"):
        query = data["q"].strip()
        queryset = queryset.filter(
            Q(name__icontains=query)
            | Q(company__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
            | Q(conversation__session_id__icontains=query)
        )
    return queryset


HANDOFF_TRANSITIONS = {
    HandoffRequest.Status.PENDING: [
        (HandoffRequest.Status.SENT, "Marcar como notificado"),
        (HandoffRequest.Status.RESOLVED, "Marcar como resolvido"),
        (HandoffRequest.Status.CANCELLED, "Cancelar handoff"),
    ],
    HandoffRequest.Status.SENT: [
        (HandoffRequest.Status.RESOLVED, "Marcar como resolvido"),
        (HandoffRequest.Status.CANCELLED, "Cancelar handoff"),
    ],
}


def get_notification_status():
    enabled = bool(getattr(settings, "LIVIA_HANDOFF_NOTIFICATIONS_ENABLED", False))
    dry_run = bool(getattr(settings, "LIVIA_HANDOFF_NOTIFICATIONS_DRY_RUN", True))
    recipient = str(getattr(settings, "LIVIA_HANDOFF_NOTIFICATION_EMAIL", "") or "").strip()

    if not enabled:
        return IntegrationStatus(
            label="Notificações de handoff",
            state="Desligadas",
            detail="Alertas automáticos desabilitados",
            tone="secondary",
        )
    if dry_run:
        return IntegrationStatus(
            label="Notificações de handoff",
            state="Dry-run",
            detail="Alertas reais não serão enviados",
            tone="warning",
        )
    if not recipient:
        return IntegrationStatus(
            label="Notificações de handoff",
            state="Configuração incompleta",
            detail="Modo real sem destinatário configurado",
            tone="danger",
        )
    return IntegrationStatus(
        label="Notificações de handoff",
        state="Ativas",
        detail="Alertas reais habilitados",
        tone="success",
    )


def get_handoff_list(form, *, page_number=1, tenant=None):
    queryset = (
        _scope_queryset(HandoffRequest.objects.select_related("tenant", "conversation", "lead_draft"), tenant)
        .prefetch_related(Prefetch("conversation__messages", queryset=Message.objects.order_by("created_at")))
        .annotate(
            status_rank=Case(
                When(status=HandoffRequest.Status.PENDING, then=Value(0)),
                When(status=HandoffRequest.Status.SENT, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            ),
            priority_rank=Case(
                When(priority=HandoffRequest.Priority.URGENT, then=Value(0)),
                When(priority=HandoffRequest.Priority.HIGH, then=Value(1)),
                When(priority=HandoffRequest.Priority.NORMAL, then=Value(2)),
                When(priority=HandoffRequest.Priority.LOW, then=Value(3)),
                default=Value(4),
                output_field=IntegerField(),
            ),
        )
        .order_by("status_rank", "priority_rank", "-created_at")
    )
    queryset = _filter_handoffs(queryset, form)
    page = Paginator(queryset, PAGE_SIZE).get_page(page_number)
    for handoff in page.object_list:
        decorate_handoff(handoff)
    return page


def get_handoff_detail(pk, *, tenant=None):
    handoff = get_object_or_404(
        _scope_queryset(HandoffRequest.objects.select_related("tenant", "conversation", "lead_draft"), tenant).prefetch_related(
            Prefetch("conversation__messages", queryset=Message.objects.order_by("created_at"))
        ),
        pk=pk,
    )
    decorate_handoff(handoff, detail=True)
    decorate_conversation(handoff.conversation)
    if handoff.lead_draft is not None:
        decorate_lead(handoff.lead_draft)
    handoff.transition_options = get_handoff_transition_options(handoff)
    handoff.notification_status = get_notification_status()
    return handoff


def get_handoff_transition_options(handoff):
    return [
        {"status": status, "label": label}
        for status, label in HANDOFF_TRANSITIONS.get(handoff.status, [])
    ]


def is_valid_handoff_transition(handoff, target_status):
    return target_status in {status for status, _label in HANDOFF_TRANSITIONS.get(handoff.status, [])}


def _filter_handoffs(queryset, form):
    if not form.is_valid():
        return queryset
    data = form.cleaned_data
    if data.get("tenant"):
        queryset = queryset.filter(tenant=data["tenant"])
    if data.get("status"):
        queryset = queryset.filter(status=data["status"])
    if data.get("priority"):
        queryset = queryset.filter(priority=data["priority"])
    elif form.data.get("priority_group") == "high":
        queryset = queryset.filter(priority__in=[HandoffRequest.Priority.HIGH, HandoffRequest.Priority.URGENT])
    if data.get("reason"):
        queryset = queryset.filter(reason=data["reason"])
    if data.get("start_date"):
        queryset = queryset.filter(created_at__date__gte=data["start_date"])
    if data.get("end_date"):
        queryset = queryset.filter(created_at__date__lte=data["end_date"])
    if data.get("q"):
        query = data["q"].strip()
        queryset = queryset.filter(
            Q(conversation__session_id__icontains=query)
            | Q(visitor_name__icontains=query)
            | Q(visitor_company__icontains=query)
            | Q(visitor_phone__icontains=query)
            | Q(visitor_email__icontains=query)
            | Q(lead_draft__name__icontains=query)
            | Q(lead_draft__company__icontains=query)
            | Q(lead_draft__email__icontains=query)
            | Q(lead_draft__phone__icontains=query)
        )
    return queryset


def _scope_queryset(queryset, tenant):
    if tenant is None:
        return queryset
    return queryset.filter(tenant=tenant)


def _active_tenant_queryset(tenant):
    if tenant is None:
        return Tenant.objects.filter(is_active=True).order_by("name")
    return Tenant.objects.filter(pk=tenant.pk, is_active=True).order_by("name")
