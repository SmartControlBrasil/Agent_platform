"""Área Comercial do operations_portal — leads, handoffs e atendimentos humanos."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from audit.models import AuditEvent
from conversations.models import HandoffRequest, Message
from leads.models import CommercialNote, LeadDraft
from leads.services.commercial_ops import (
    LOST_REASONS,
    add_commercial_note,
    assign_handoff,
    assign_lead,
    change_lead_commercial_status,
    mask_email,
    mask_phone,
    normalize_phone_for_whatsapp,
)
from operations_portal.access import portal_template_context, require_portal_capability, resolve_portal_access
from operations_portal.selectors import _scope_queryset, clean_querystring
from tenants.access import CAPABILITY_COMMERCIAL_MANAGE, CAPABILITY_COMMERCIAL_VIEW

User = get_user_model()
PAGE_SIZE = 25


def _can_see_full_pii(access) -> bool:
    return access.is_global or CAPABILITY_COMMERCIAL_MANAGE in access.capabilities or access.user.is_superuser


def _notification_label_for_lead(lead: LeadDraft) -> str:
    data = dict(lead.qualification_data or {})
    if data.get("lead_notification_sent_at"):
        return "sent"
    if data.get("lead_notification_dry_run_at") or data.get("lead_notification_dry_run"):
        return "dry-run"
    if lead.dispatch_status == LeadDraft.DispatchStatus.FAILED:
        return "failed"
    if lead.dispatch_status == LeadDraft.DispatchStatus.DRY_RUN:
        return "dry-run"
    if lead.dispatch_status == LeadDraft.DispatchStatus.DELIVERED:
        return "sent"
    if lead.dispatch_status in {LeadDraft.DispatchStatus.PENDING, LeadDraft.DispatchStatus.RETRYING}:
        return "pending"
    return "—"


def _notification_label_for_handoff(handoff: HandoffRequest) -> str:
    meta = dict(handoff.metadata or {})
    if meta.get("handoff_notification_dry_run"):
        return "dry-run"
    if meta.get("handoff_notification_sent_at"):
        return "sent"
    if handoff.dispatch_state == HandoffRequest.DispatchState.FAILED:
        return "failed"
    if handoff.dispatch_state == HandoffRequest.DispatchState.DRY_RUN:
        return "dry-run"
    if handoff.dispatch_state == HandoffRequest.DispatchState.DELIVERED:
        return "sent"
    return handoff.dispatch_state or "—"


def _decorate_lead_row(lead: LeadDraft, *, full_pii: bool):
    lead.commercial_status_label = lead.get_commercial_status_display()
    lead.notification_label = _notification_label_for_lead(lead)
    lead.source_page = getattr(lead.conversation, "source_page", "") if lead.conversation_id else ""
    lead.display_phone = lead.phone if full_pii else mask_phone(lead.phone)
    lead.display_email = lead.email if full_pii else mask_email(lead.email)
    lead.assignee_label = getattr(lead.assigned_to, "username", "") or "—"
    lead.wa_link = ""
    if full_pii and lead.phone:
        digits = normalize_phone_for_whatsapp(lead.phone)
        if digits:
            lead.wa_link = f"https://wa.me/{digits}"
    lead.mailto_link = f"mailto:{lead.email}" if full_pii and lead.email else ""
    if lead.assigned_at and lead.created_at:
        lead.time_to_assign = lead.assigned_at - lead.created_at
    else:
        lead.time_to_assign = None
    if lead.first_human_action_at and lead.created_at:
        lead.time_to_first_action = lead.first_human_action_at - lead.created_at
    else:
        lead.time_to_first_action = None
    return lead


def _lead_queryset(tenant):
    return (
        _scope_queryset(LeadDraft.objects.all(), tenant)
        .select_related("tenant", "conversation", "assigned_to")
        .prefetch_related("handoff_requests")
    )


def _handoff_queryset(tenant):
    return (
        _scope_queryset(HandoffRequest.objects.all(), tenant)
        .select_related("tenant", "conversation", "lead_draft", "assigned_to")
    )


@login_required(login_url="/admin/login/")
def commercial_dashboard(request):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    leads = _lead_queryset(access.tenant)
    handoffs = _handoff_queryset(access.tenant)
    period_days = int(request.GET.get("days") or 30)
    since = timezone.now() - timedelta(days=max(1, min(period_days, 365)))
    leads_period = leads.filter(created_at__gte=since)
    cards = {
        "new": leads_period.filter(commercial_status=LeadDraft.CommercialStatus.NEW).count(),
        "in_progress": leads_period.filter(
            commercial_status__in=[
                LeadDraft.CommercialStatus.CONTACT_PENDING,
                LeadDraft.CommercialStatus.IN_PROGRESS,
            ]
        ).count(),
        "qualified": leads_period.filter(commercial_status=LeadDraft.CommercialStatus.QUALIFIED).count(),
        "won": leads_period.filter(commercial_status=LeadDraft.CommercialStatus.WON).count(),
        "lost": leads_period.filter(commercial_status=LeadDraft.CommercialStatus.LOST).count(),
        "handoffs_pending": handoffs.filter(status=HandoffRequest.Status.PENDING).count(),
    }
    recent_leads = [_decorate_lead_row(lead, full_pii=_can_see_full_pii(access)) for lead in leads_period.order_by("-created_at")[:8]]
    pending_handoffs = list(handoffs.filter(status__in=[HandoffRequest.Status.PENDING, HandoffRequest.Status.SENT]).order_by("-priority", "-created_at")[:8])
    context = {
        "active_section": "comercial",
        "cards": cards,
        "period_days": period_days,
        "recent_leads": recent_leads,
        "pending_handoffs": pending_handoffs,
        "can_manage": CAPABILITY_COMMERCIAL_MANAGE in access.capabilities or access.is_global,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/commercial/dashboard.html", context)


@login_required(login_url="/admin/login/")
def commercial_lead_list(request):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    qs = _lead_queryset(access.tenant)
    status = (request.GET.get("status") or "").strip()
    assignee = (request.GET.get("assigned_to") or "").strip()
    origin = (request.GET.get("origin") or "").strip()
    q = (request.GET.get("q") or "").strip()
    tenant_slug = (request.GET.get("tenant") or "").strip()
    if status:
        qs = qs.filter(commercial_status=status)
    if assignee == "me":
        qs = qs.filter(assigned_to=request.user)
    elif assignee == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)
    elif assignee.isdigit():
        qs = qs.filter(assigned_to_id=int(assignee))
    if origin:
        qs = qs.filter(conversation__source_page__icontains=origin[:120])
    if tenant_slug and access.is_global:
        qs = qs.filter(tenant__slug=tenant_slug)
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(company__icontains=q)
            | Q(email__icontains=q)
            | Q(phone__icontains=q)
            | Q(need_summary__icontains=q)
        )
    page = Paginator(qs.order_by("-updated_at"), PAGE_SIZE).get_page(request.GET.get("page") or 1)
    full_pii = _can_see_full_pii(access)
    for lead in page.object_list:
        _decorate_lead_row(lead, full_pii=full_pii)
    assignees = User.objects.filter(is_active=True).order_by("username")[:100]
    context = {
        "active_section": "comercial",
        "page_obj": page,
        "querystring": clean_querystring(request.GET),
        "filters": {
            "status": status,
            "assigned_to": assignee,
            "origin": origin,
            "q": q,
            "tenant": tenant_slug,
        },
        "status_choices": LeadDraft.CommercialStatus.choices,
        "assignees": assignees,
        "can_manage": CAPABILITY_COMMERCIAL_MANAGE in access.capabilities or access.is_global,
        "tenants": access.accessible_tenants if access.is_global else [],
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/commercial/lead_list.html", context)


@login_required(login_url="/admin/login/")
def commercial_lead_detail(request, pk):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    lead = get_object_or_404(
        _lead_queryset(access.tenant).prefetch_related(
            Prefetch("commercial_notes", queryset=CommercialNote.objects.select_related("author").order_by("-created_at")),
            "handoff_requests",
        ),
        pk=pk,
    )
    full_pii = _can_see_full_pii(access)
    _decorate_lead_row(lead, full_pii=full_pii)
    messages_qs = []
    if lead.conversation_id:
        messages_qs = list(
            Message.objects.filter(conversation_id=lead.conversation_id)
            .exclude(role=Message.Role.SYSTEM)
            .order_by("created_at", "id")[:400]
        )
    audits = list(
        AuditEvent.objects.filter(tenant_id=lead.tenant_id, object_id=str(lead.pk))
        .filter(action__startswith="lead.")
        .order_by("-created_at")[:40]
    )
    assignees = User.objects.filter(is_active=True).order_by("username")[:100]
    notification_email = ""
    try:
        profile = getattr(lead.tenant, "assistant_profile", None)
        notification_email = getattr(profile, "notification_email", "") or ""
    except Exception:
        notification_email = ""
    context = {
        "active_section": "comercial",
        "lead": lead,
        "transcript": messages_qs,
        "notes": list(lead.commercial_notes.all()[:50]),
        "audits": audits,
        "status_choices": LeadDraft.CommercialStatus.choices,
        "lost_reasons": LOST_REASONS,
        "assignees": assignees,
        "can_manage": CAPABILITY_COMMERCIAL_MANAGE in access.capabilities or access.is_global,
        "full_pii": full_pii,
        "handoffs": list(lead.handoff_requests.all()[:10]),
        "notification_email": notification_email,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/commercial/lead_detail.html", context)


@login_required(login_url="/admin/login/")
@require_POST
def commercial_lead_assign(request, pk):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    lead = get_object_or_404(_lead_queryset(access.tenant), pk=pk)
    user_id = request.POST.get("assigned_to") or request.user.pk
    user = get_object_or_404(User, pk=user_id, is_active=True)
    assign_lead(lead=lead, user=user, actor=request.user)
    messages.success(request, f"Lead atribuído a {user.username}.")
    return redirect("operations_portal:commercial_lead_detail", pk=lead.pk)


@login_required(login_url="/admin/login/")
@require_POST
def commercial_lead_status(request, pk):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    lead = get_object_or_404(_lead_queryset(access.tenant), pk=pk)
    new_status = (request.POST.get("commercial_status") or "").strip()
    note = (request.POST.get("note") or "").strip()
    lost_reason = (request.POST.get("lost_reason") or "").strip()
    try:
        change_lead_commercial_status(
            lead=lead,
            new_status=new_status,
            actor=request.user,
            note=note,
            lost_reason=lost_reason,
        )
        messages.success(request, "Status comercial atualizado.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("operations_portal:commercial_lead_detail", pk=lead.pk)


@login_required(login_url="/admin/login/")
@require_POST
def commercial_lead_note(request, pk):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    lead = get_object_or_404(_lead_queryset(access.tenant), pk=pk)
    body = (request.POST.get("body") or "").strip()
    try:
        add_commercial_note(lead=lead, author=request.user, body=body)
        messages.success(request, "Nota registrada.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("operations_portal:commercial_lead_detail", pk=lead.pk)


@login_required(login_url="/admin/login/")
def commercial_handoff_list(request):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    qs = _handoff_queryset(access.tenant)
    status = (request.GET.get("status") or "").strip()
    q = (request.GET.get("q") or "").strip()
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(
            Q(visitor_name__icontains=q)
            | Q(visitor_email__icontains=q)
            | Q(visitor_phone__icontains=q)
            | Q(summary__icontains=q)
        )
    page = Paginator(qs.order_by("-created_at"), PAGE_SIZE).get_page(request.GET.get("page") or 1)
    full_pii = _can_see_full_pii(access)
    for item in page.object_list:
        item.notification_label = _notification_label_for_handoff(item)
        item.display_phone = item.visitor_phone if full_pii else mask_phone(item.visitor_phone)
        item.display_email = item.visitor_email if full_pii else mask_email(item.visitor_email)
        item.assignee_label = getattr(item.assigned_to, "username", "") or "—"
    context = {
        "active_section": "comercial",
        "page_obj": page,
        "querystring": clean_querystring(request.GET),
        "filters": {"status": status, "q": q},
        "status_choices": HandoffRequest.Status.choices,
        "can_manage": CAPABILITY_COMMERCIAL_MANAGE in access.capabilities or access.is_global,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/commercial/handoff_list.html", context)


@login_required(login_url="/admin/login/")
def commercial_handoff_detail(request, pk):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    handoff = get_object_or_404(
        _handoff_queryset(access.tenant).prefetch_related(
            Prefetch(
                "commercial_notes",
                queryset=CommercialNote.objects.select_related("author").order_by("-created_at"),
            )
        ),
        pk=pk,
    )
    full_pii = _can_see_full_pii(access)
    transcript = list(
        Message.objects.filter(conversation_id=handoff.conversation_id)
        .exclude(role=Message.Role.SYSTEM)
        .order_by("created_at", "id")[:400]
    )
    handoff.notification_label = _notification_label_for_handoff(handoff)
    handoff.display_phone = handoff.visitor_phone if full_pii else mask_phone(handoff.visitor_phone)
    handoff.display_email = handoff.visitor_email if full_pii else mask_email(handoff.visitor_email)
    wa = normalize_phone_for_whatsapp(handoff.visitor_phone) if full_pii else ""
    context = {
        "active_section": "comercial",
        "handoff": handoff,
        "transcript": transcript,
        "notes": list(handoff.commercial_notes.all()[:50]),
        "can_manage": CAPABILITY_COMMERCIAL_MANAGE in access.capabilities or access.is_global,
        "full_pii": full_pii,
        "wa_link": f"https://wa.me/{wa}" if wa else "",
        "mailto_link": f"mailto:{handoff.visitor_email}" if full_pii and handoff.visitor_email else "",
        "assignees": User.objects.filter(is_active=True).order_by("username")[:100],
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/commercial/handoff_detail.html", context)


@login_required(login_url="/admin/login/")
@require_POST
def commercial_handoff_assign(request, pk):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    handoff = get_object_or_404(_handoff_queryset(access.tenant), pk=pk)
    user_id = request.POST.get("assigned_to") or request.user.pk
    user = get_object_or_404(User, pk=user_id, is_active=True)
    assign_handoff(handoff=handoff, user=user, actor=request.user)
    messages.success(request, f"Handoff atribuído a {user.username}.")
    return redirect("operations_portal:commercial_handoff_detail", pk=handoff.pk)


@login_required(login_url="/admin/login/")
@require_POST
def commercial_handoff_note(request, pk):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    handoff = get_object_or_404(_handoff_queryset(access.tenant), pk=pk)
    try:
        add_commercial_note(handoff=handoff, author=request.user, body=(request.POST.get("body") or "").strip())
        messages.success(request, "Nota registrada.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("operations_portal:commercial_handoff_detail", pk=handoff.pk)


@login_required(login_url="/admin/login/")
def commercial_attendances(request):
    """Fila unificada: handoffs urgentes → normais → leads novos."""
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    handoffs = list(
        _handoff_queryset(access.tenant)
        .filter(status__in=[HandoffRequest.Status.PENDING, HandoffRequest.Status.SENT])
        .order_by("-priority", "-created_at")[:100]
    )
    urgent = [h for h in handoffs if h.priority == HandoffRequest.Priority.URGENT]
    normal = [h for h in handoffs if h.priority != HandoffRequest.Priority.URGENT]
    new_leads = list(
        _lead_queryset(access.tenant)
        .filter(commercial_status=LeadDraft.CommercialStatus.NEW)
        .order_by("-created_at")[:50]
    )
    full_pii = _can_see_full_pii(access)
    for lead in new_leads:
        _decorate_lead_row(lead, full_pii=full_pii)
    for item in urgent + normal:
        item.notification_label = _notification_label_for_handoff(item)
    context = {
        "active_section": "comercial",
        "urgent_handoffs": urgent,
        "normal_handoffs": normal,
        "new_leads": new_leads,
        "can_manage": CAPABILITY_COMMERCIAL_MANAGE in access.capabilities or access.is_global,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/commercial/attendances.html", context)
