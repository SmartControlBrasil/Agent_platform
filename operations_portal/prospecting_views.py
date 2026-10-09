from __future__ import annotations

import logging
import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Exists, Max, OuterRef, Q, Subquery
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from audit.services import record_audit_event
from operations_portal.access import portal_template_context, require_portal_capability, resolve_portal_access
from operations_portal.prospecting_forms import (
    ProspectActivityForm,
    ProspectContactForm,
    ProspectContactOutcomeForm,
    FollowUpQueueFilterForm,
    MyQueueFilterForm,
    ProspectFollowUpForm,
    ProspectOutreachDraftForm,
    ProspectEnrichmentCreateForm,
    ProspectFilterForm,
    ProspectQualificationForm,
    ProspectingSearchPlanForm,
    ProspectingSearchQueryReviewForm,
    SearchResultBulkActionForm,
    SearchResultFilterForm,
    SearchRunFilterForm,
)
from operations_portal.selectors import clean_querystring
from projects.models import Project
from prospecting.application.activities import (
    create_prospect_activity,
    delete_prospect_activity,
    update_prospect_activity,
)
from prospecting.application.outreach_drafts import (
    archive_outreach_draft,
    create_outreach_draft,
    mark_outreach_draft_ready,
    restore_archived_outreach_draft,
    revert_outreach_draft_to_draft,
    update_outreach_draft,
)
from prospecting.application.outreach_sends import retry_outreach_send, send_outreach_draft_email
from prospecting.application.contact_outcomes import record_prospect_contact_outcome
from prospecting.application.commercial_queue import (
    CommercialQueueCategory,
    CommercialQueueCounters,
    INBOUND_KIND_LABELS,
    SOURCE_INBOUND_LIVIA,
    build_commercial_queue,
    commercial_queue_counters_from_items,
)
from prospecting.application.follow_up_queue import (
    apply_follow_up_queue_filters,
    annotate_follow_up_sort_bucket,
    base_follow_up_queue_queryset,
    classify_follow_up_temporal,
    compute_follow_up_queue_counters,
    order_follow_up_queue,
)
from prospecting.application.follow_ups import (
    cancel_prospect_follow_up,
    complete_prospect_follow_up,
    create_prospect_follow_up,
)
from prospecting.application.contacts import (
    create_prospect_contact,
    delete_prospect_contact,
    update_prospect_contact,
)
from prospecting.application.enrichments import add_prospect_enrichment
from prospecting.application.prospects import promote_search_result_to_prospect
from prospecting.application.qualification import qualify_prospect
from prospecting.application.execution_recovery import (
    ProspectingExecutionRecoveryError,
    cancel_search_run,
    redispatch_search_run,
    retry_search_run,
)
from prospecting.application.review import (
    ProspectingReviewError,
    bulk_ignore_search_results,
    bulk_promote_search_results,
    mark_search_result_ignored,
    restore_search_result_to_unreviewed,
)
from prospecting.application.search_runs import (
    ProspectingSearchRunError,
    build_search_plan_for_manual_run,
    create_and_dispatch_search_run,
    synchronize_search_run,
)
from prospecting.application.website_enrichment import enrich_prospect_from_website
from prospecting.application.website_enrichment_errors import website_enrichment_error_message
from prospecting.models import (
    Prospect,
    ProspectActivity,
    ProspectContact,
    ProspectContactOutcome,
    ProspectEnrichment,
    ProspectFollowUp,
    ProspectOutreachDraft,
    ProspectOutreachSend,
    ProspectSource,
    SearchResult,
    SearchRun,
    SearchRunExecutionAttempt,
)
from tenants.access import CAPABILITY_COMMERCIAL_MANAGE, CAPABILITY_COMMERCIAL_VIEW
from tools.application.executor_presence import executor_presence
from tools.infrastructure.validation import summarize_tool_execution
from tools.models import ToolExecution, ToolExecutorCapability

ACTION_PROSPECT_PROMOTED = "prospecting.search_result.promoted"
ACTION_PROSPECT_ENRICHMENT_ADDED = "prospecting.enrichment.added"
ACTION_PROSPECT_WEBSITE_ENRICHED = "prospecting.enrichment.website_requested"
BULK_WEBSITE_ENRICHMENT_LIMIT = 10
SESSION_SEARCH_DRAFTS_KEY = "operations_portal_prospecting_drafts"
SESSION_SEARCH_DRAFT_EXECUTIONS_KEY = "operations_portal_prospecting_draft_executions"
logger = logging.getLogger(__name__)


SEARCH_RUN_STATUS_UI = {
    SearchRun.Status.PENDING: {"label": "Preparando", "tone": "secondary"},
    SearchRun.Status.DISPATCHED: {"label": "Aguardando executor", "tone": "info"},
    SearchRun.Status.RUNNING: {"label": "Executando", "tone": "primary"},
    SearchRun.Status.COMPLETED: {"label": "Concluída", "tone": "success"},
    SearchRun.Status.FAILED: {"label": "Falhou", "tone": "danger"},
    SearchRun.Status.CANCELLED: {"label": "Cancelada", "tone": "warning"},
}
EXECUTION_ERROR_MESSAGES = {
    "results_not_loaded": "Os resultados do Google Maps não ficaram disponíveis a tempo.",
    "invalid_tool_result": "O executor retornou um resultado inválido.",
    "google_challenge": "O Google Maps apresentou bloqueio/desafio durante a execução.",
    "navigation_timeout": "A navegação do Google Maps excedeu o tempo esperado.",
    "page_structure_changed": "A estrutura da página do Google Maps não foi reconhecida.",
    "executor_unavailable": "Nenhum executor compatível está disponível.",
    "tool_execution_failed": "A execução da pesquisa falhou.",
}


def _project_queryset_for_tenant(tenant):
    if tenant is None:
        return Project.objects.filter(is_active=True).order_by("tenant__name", "name")
    return Project.objects.filter(tenant=tenant, is_active=True).order_by("name")


def _search_run_queryset(tenant):
    queryset = SearchRun.objects.select_related("tenant", "project", "agent_installation").prefetch_related("results")
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    return queryset


def _search_result_queryset(tenant):
    queryset = SearchResult.objects.select_related("tenant", "search_run", "search_run__project")
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    return queryset


def _resolve_search_result_action_targets(*, access, request, search_run):
    selected_ids = request.POST.getlist("selected_result_ids")
    if not selected_ids:
        return selected_ids, SearchResult.objects.none()
    queryset = _search_result_queryset(access.tenant).filter(search_run=search_run, pk__in=selected_ids)
    return selected_ids, queryset


def _build_search_result_queryset(search_run):
    promoted_subquery = ProspectSource.objects.filter(search_result_id=OuterRef("pk"))
    return search_run.results.annotate(is_promoted=Exists(promoted_subquery))


def _prospect_queryset(tenant):
    queryset = Prospect.objects.select_related("tenant").prefetch_related("sources", "enrichments", "contacts")
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    return queryset


def _contact_queryset(tenant):
    queryset = ProspectContact.objects.select_related("prospect", "tenant")
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    return queryset


def _activity_queryset(tenant):
    queryset = ProspectActivity.objects.select_related("prospect", "tenant", "contact", "created_by")
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    return queryset


def _outreach_draft_queryset(tenant):
    queryset = ProspectOutreachDraft.objects.select_related(
        "prospect",
        "tenant",
        "contact",
        "created_by",
        "outreach_send",
        "outreach_send__requested_by",
    )
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    return queryset


def _outreach_send_queryset(tenant):
    queryset = ProspectOutreachSend.objects.select_related("draft", "prospect", "tenant", "contact", "requested_by")
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    return queryset


def _follow_up_queryset(tenant):
    queryset = ProspectFollowUp.objects.select_related("prospect", "tenant", "contact", "created_by", "completed_by")
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    return queryset


def _follow_up_queue_tenant_scope(access):
    if access.tenant is not None:
        return access.tenant, None
    if access.is_global or access.show_all_tenants:
        tenant_ids = [item.pk for item in access.accessible_tenants]
        return None, tenant_ids or None
    return None, [item.pk for item in access.accessible_tenants]


def _can_mutate_follow_up_queue(access):
    if access.is_global or access.tenant is None:
        return False
    return CAPABILITY_COMMERCIAL_MANAGE in access.capabilities


_FOLLOW_UP_QUEUE_RETURN_VIEWS = frozenset(
    {
        "prospecting_follow_up_queue",
        "prospecting_my_queue",
    }
)


def _follow_up_queue_redirect(request):
    view_name = (request.POST.get("return_view") or "prospecting_follow_up_queue").strip()
    if view_name not in _FOLLOW_UP_QUEUE_RETURN_VIEWS:
        view_name = "prospecting_follow_up_queue"
    qs = (request.POST.get("queue_querystring") or "").strip()
    url = reverse(f"operations_portal:{view_name}")
    if qs:
        return f"{url}?{qs}"
    return url


MY_QUEUE_CATEGORY_LABELS = {
    CommercialQueueCategory.INBOUND_HANDOFF: "Inbound comercial",
    CommercialQueueCategory.FOLLOW_UP_OVERDUE: "Acompanhamento atrasado",
    CommercialQueueCategory.FOLLOW_UP_TODAY: "Acompanhamento hoje",
    CommercialQueueCategory.OUTREACH_PENDING: "Abordagem pendente",
    CommercialQueueCategory.WAITING_OUTCOME: "Registrar resultado",
    CommercialQueueCategory.MISSING_CONTACT: "Contato necessário",
    CommercialQueueCategory.READY_FOR_OUTREACH: "Pronto para abordagem",
}

MY_QUEUE_ACTION_LABELS = {
    CommercialQueueCategory.INBOUND_HANDOFF: "Atender lead inbound",
    CommercialQueueCategory.FOLLOW_UP_OVERDUE: "Executar acompanhamento atrasado",
    CommercialQueueCategory.FOLLOW_UP_TODAY: "Executar acompanhamento de hoje",
    CommercialQueueCategory.OUTREACH_PENDING: "Continuar abordagem",
    CommercialQueueCategory.WAITING_OUTCOME: "Registrar resultado da abordagem",
    CommercialQueueCategory.MISSING_CONTACT: "Criar contato utilizável",
    CommercialQueueCategory.READY_FOR_OUTREACH: "Preparar primeira abordagem",
}


def _apply_my_queue_filters(items, *, category="", priority="", prospect_q=""):
    category = (category or "").strip()
    if category and category in CommercialQueueCategory.ALL:
        items = [item for item in items if item.category == category]
    priority = (priority or "").strip().upper()
    if priority == "UNSET":
        items = [item for item in items if item.priority == Prospect.Priority.UNSET]
    elif priority in {Prospect.Priority.HIGH, Prospect.Priority.MEDIUM, Prospect.Priority.LOW}:
        items = [item for item in items if item.priority == priority]
    prospect_q = (prospect_q or "").strip()
    if prospect_q:
        needle = prospect_q.casefold()
        items = [
            item
            for item in items
            if needle in item.display_name.casefold()
            or needle in (item.company or "").casefold()
            or needle in (item.email or "").casefold()
            or needle in (item.phone or "").casefold()
        ]
    return items


def _waiting_outcome_send_ids(*, tenant, tenant_ids, prospect_ids):
    if not prospect_ids:
        return {}
    queryset = ProspectOutreachSend.objects.filter(
        prospect_id__in=prospect_ids,
        status=ProspectOutreachSend.Status.SENT,
    ).annotate(
        has_outcome=Exists(
            ProspectContactOutcome.objects.filter(
                outreach_send_id=OuterRef("pk"),
            )
        )
    ).filter(has_outcome=False)
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    elif tenant_ids is not None:
        queryset = queryset.filter(tenant_id__in=tenant_ids)
    send_by_prospect: dict = {}
    for send in queryset.order_by("prospect_id", "sent_at", "created_at"):
        if send.prospect_id not in send_by_prospect:
            send_by_prospect[send.prospect_id] = send.id
    return send_by_prospect


def _build_my_queue_rows(*, items, tenant, tenant_ids, tenant_names):
    follow_up_ids = [item.follow_up_id for item in items if item.follow_up_id]
    follow_up_qs = ProspectFollowUp.objects.filter(pk__in=follow_up_ids).select_related("prospect", "contact", "tenant")
    if tenant is not None:
        follow_up_qs = follow_up_qs.filter(tenant=tenant)
    elif tenant_ids is not None:
        follow_up_qs = follow_up_qs.filter(tenant_id__in=tenant_ids)
    follow_ups = {item.id: item for item in follow_up_qs}

    waiting_prospect_ids = [
        item.prospect_id
        for item in items
        if item.prospect_id and item.category == CommercialQueueCategory.WAITING_OUTCOME
    ]
    waiting_send_ids = _waiting_outcome_send_ids(tenant=tenant, tenant_ids=tenant_ids, prospect_ids=waiting_prospect_ids)

    rows = []
    for item in items:
        rows.append(
            {
                "item": item,
                "category_label": MY_QUEUE_CATEGORY_LABELS.get(item.category, item.category),
                "action_label": MY_QUEUE_ACTION_LABELS.get(item.category, ""),
                "follow_up": follow_ups.get(item.follow_up_id),
                "waiting_send_id": waiting_send_ids.get(item.prospect_id),
                "tenant_name": tenant_names.get(item.tenant_id, ""),
                "inbound_kind_label": INBOUND_KIND_LABELS.get(item.inbound_kind, ""),
            }
        )
    return rows


def _my_queue_primary_cta_url(*, item, waiting_send_id):
    if getattr(item, "source", "") == SOURCE_INBOUND_LIVIA:
        if item.lead_draft_id:
            return reverse("operations_portal:commercial_lead_detail", args=[item.lead_draft_id])
        if item.handoff_id:
            return reverse("operations_portal:commercial_handoff_detail", args=[item.handoff_id])
        return reverse("operations_portal:commercial_lead_list")
    detail = reverse("operations_portal:prospecting_prospect_detail", args=[item.prospect_id])
    if item.category in {
        CommercialQueueCategory.FOLLOW_UP_OVERDUE,
        CommercialQueueCategory.FOLLOW_UP_TODAY,
    }:
        return detail
    if item.category == CommercialQueueCategory.OUTREACH_PENDING:
        return f"{detail}?outreach_status=READY"
    if item.category == CommercialQueueCategory.WAITING_OUTCOME and waiting_send_id:
        return f"{detail}?record_outcome_send={waiting_send_id}"
    if item.category == CommercialQueueCategory.MISSING_CONTACT:
        return f"{detail}#contact-create"
    if item.category == CommercialQueueCategory.READY_FOR_OUTREACH:
        return f"{detail}?new_outreach=1"
    return detail


def _my_queue_primary_cta_label(*, category, item=None):
    if category == CommercialQueueCategory.INBOUND_HANDOFF:
        if item is not None and item.lead_draft_id:
            return "Abrir lead"
        return "Abrir atendimento"
    if category in {CommercialQueueCategory.FOLLOW_UP_OVERDUE, CommercialQueueCategory.FOLLOW_UP_TODAY}:
        return "Abrir prospect"
    if category == CommercialQueueCategory.OUTREACH_PENDING:
        return "Continuar abordagem"
    if category == CommercialQueueCategory.WAITING_OUTCOME:
        return "Registrar resultado"
    if category == CommercialQueueCategory.MISSING_CONTACT:
        return "Criar contato"
    if category == CommercialQueueCategory.READY_FOR_OUTREACH:
        return "Preparar abordagem"
    return "Abrir prospect"


def _resolve_viewing_outreach_send(tenant, prospect, send_id_raw):
    send_id = (send_id_raw or "").strip()
    if not send_id:
        return None
    return get_object_or_404(_outreach_send_queryset(tenant), pk=send_id, prospect=prospect)


def _search_run_ui(status):
    return SEARCH_RUN_STATUS_UI.get(status, {"label": status, "tone": "secondary"})


def _enrich_run_ui(search_run):
    ui = _search_run_ui(search_run.status)
    search_run.status_label = ui["label"]
    search_run.status_tone = ui["tone"]
    return search_run


def _execution_error_message(execution):
    if execution is None:
        return ""
    code = (execution.error_code or "").strip().lower()
    if code and code in EXECUTION_ERROR_MESSAGES:
        return EXECUTION_ERROR_MESSAGES[code]
    if execution.status == ToolExecution.Status.EXPIRED:
        return "A execução expirou antes de ser processada. Use retry para criar uma nova tentativa."
    if execution.status == ToolExecution.Status.CANCELLED:
        return "A pesquisa foi cancelada antes da conclusão."
    if execution.status == ToolExecution.Status.FAILED:
        return "Não foi possível concluir a pesquisa."
    return ""


def _attempt_status_label(status):
    labels = {
        ToolExecution.Status.PENDING: "Pendente",
        ToolExecution.Status.DISPATCHED: "Aguardando executor",
        ToolExecution.Status.RUNNING: "Executando",
        ToolExecution.Status.SUCCEEDED: "Concluída",
        ToolExecution.Status.FAILED: "Falhou",
        ToolExecution.Status.CANCELLED: "Cancelada",
        ToolExecution.Status.EXPIRED: "Expirada",
    }
    return labels.get(status, status)


def _ensure_attempt_tracking(search_run):
    attempts = search_run.execution_attempts.select_related("tool_execution", "tool_execution__executor").order_by("attempt_number")
    if attempts.exists() or not search_run.agent_platform_execution_id:
        return attempts
    execution = ToolExecution.objects.filter(
        pk=search_run.agent_platform_execution_id,
        tenant=search_run.tenant,
        project=search_run.project,
        agent_installation=search_run.agent_installation,
    ).select_related("executor").first()
    if execution is not None:
        SearchRunExecutionAttempt.objects.get_or_create(
            search_run=search_run,
            tool_execution=execution,
            defaults={"tenant": search_run.tenant, "attempt_number": 1},
        )
    return search_run.execution_attempts.select_related("tool_execution", "tool_execution__executor").order_by("attempt_number")



def _execution_metadata_view(execution):
    payload = execution.result_payload if execution and isinstance(execution.result_payload, dict) else {}
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    return {
        "progress": _format_execution_progress(progress),
        "diagnostics": _format_execution_diagnostics(diagnostics),
    }


def _format_execution_progress(progress):
    if not progress:
        return None
    return {
        "stage": progress.get("stage") or "—",
        "current_query": progress.get("current_query") or "—",
        "detected_results": progress.get("detected_results", "—"),
        "processed_results": progress.get("processed_results", "—"),
        "last_activity_at": progress.get("last_activity_at") or "—",
    }


def _format_execution_diagnostics(diagnostics):
    if not diagnostics:
        return []
    labels = (
        ("stage", "Etapa"),
        ("current_query", "Query atual"),
        ("pathname", "Pathname"),
        ("search", "Parâmetros URL"),
        ("feed_found", "Feed encontrado"),
        ("article_count", "role=article"),
        ("place_anchor_count", "Links de place"),
        ("zero_results", "Sem resultados"),
        ("consent_screen", "Consentimento"),
        ("captcha_or_blocked", "Captcha/bloqueio"),
        ("detected_results", "Resultados detectados"),
        ("processed_results", "Resultados processados"),
        ("last_activity_at", "Última atividade"),
    )
    rows = []
    for key, label in labels:
        if key in diagnostics:
            rows.append({"label": label, "value": _display_diagnostic_value(diagnostics[key])})
    return rows


def _display_diagnostic_value(value):
    if value is True:
        return "Sim"
    if value is False:
        return "Não"
    if value in (None, ""):
        return "—"
    return value


def _compatible_executor_snapshot(search_run, execution):
    if execution is None:
        return []
    capabilities = (
        ToolExecutorCapability.objects.select_related("executor")
        .filter(
            tool_definition=execution.tool_definition,
            executor__tenant=search_run.tenant,
            executor__is_active=True,
            is_enabled=True,
        )
        .order_by("executor__name")
    )
    snapshot = []
    for capability in capabilities:
        presence = executor_presence(capability.executor)
        snapshot.append(
            {
                "executor": capability.executor,
                "presence_label": presence.label,
                "is_online": presence.is_online,
                "last_seen_at": capability.executor.last_seen_at,
            }
        )
    return snapshot


def _drafts(request):
    return dict(request.session.get(SESSION_SEARCH_DRAFTS_KEY, {}))


def _executed_drafts(request):
    return dict(request.session.get(SESSION_SEARCH_DRAFT_EXECUTIONS_KEY, {}))


def _save_draft(request, payload):
    drafts = _drafts(request)
    draft_id = uuid.uuid4().hex
    drafts[draft_id] = payload
    request.session[SESSION_SEARCH_DRAFTS_KEY] = drafts
    request.session.modified = True
    return draft_id


def _mark_draft_executed(request, *, draft_id, search_run_id):
    drafts = _drafts(request)
    drafts.pop(draft_id, None)
    executed = _executed_drafts(request)
    executed[draft_id] = str(search_run_id)
    request.session[SESSION_SEARCH_DRAFTS_KEY] = drafts
    request.session[SESSION_SEARCH_DRAFT_EXECUTIONS_KEY] = executed
    request.session.modified = True


def _ensure_posted_tenant_matches(access, request):
    posted_tenant = request.POST.get("tenant")
    if posted_tenant and str(posted_tenant) != str(access.tenant.pk):
        raise PermissionDenied


def _render_search_run_create(request, *, access, form, draft=None, review_form=None):
    context = {
        "active_section": "prospeccao",
        "form": form,
        "review_form": review_form,
        "draft": draft,
        "can_manage": access.is_global or CAPABILITY_COMMERCIAL_MANAGE in access.capabilities,
        "has_projects": _project_queryset_for_tenant(access.tenant).exists(),
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/search_run_create.html", context)


def _sync_search_run_for_display(search_run):
    if search_run.status in {SearchRun.Status.COMPLETED, SearchRun.Status.FAILED, SearchRun.Status.CANCELLED}:
        return search_run
    try:
        return synchronize_search_run(search_run=search_run)
    except (ValidationError, ProspectingSearchRunError):
        logger.exception("prospecting_search_run_display_sync_failed", extra={"search_run_id": str(search_run.id)})
        return search_run


@login_required(login_url="/admin/login/")
def prospecting_search_run_list(request):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    queryset = _search_run_queryset(access.tenant)
    form = SearchRunFilterForm(request.GET or None, project_queryset=_project_queryset_for_tenant(access.tenant))
    if form.is_valid():
        status = form.cleaned_data.get("status")
        project = form.cleaned_data.get("project")
        query = (form.cleaned_data.get("q") or "").strip()
        if status:
            queryset = queryset.filter(status=status)
        if project:
            queryset = queryset.filter(project=project)
        if query:
            queryset = queryset.filter(
                Q(target_region__icontains=query)
                | Q(target_market__icontains=query)
                | Q(target_profile__icontains=query)
                | Q(objective__icontains=query)
            )
    page_obj = Paginator(queryset.order_by("-created_at"), 25).get_page(request.GET.get("page") or 1)
    page_obj.object_list = list(page_obj.object_list)
    for index, run in enumerate(page_obj.object_list):
        run = _sync_search_run_for_display(run)
        _enrich_run_ui(run)
        page_obj.object_list[index] = run
    context = {
        "active_section": "prospeccao",
        "form": form,
        "page_obj": page_obj,
        "querystring": clean_querystring(request.GET),
        "can_manage": access.is_global or CAPABILITY_COMMERCIAL_MANAGE in access.capabilities,
        "can_create": bool(access.tenant) and not access.is_global and (CAPABILITY_COMMERCIAL_MANAGE in access.capabilities),
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/search_run_list.html", context)


@login_required(login_url="/admin/login/")
def prospecting_search_run_create(request):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    project_queryset = _project_queryset_for_tenant(access.tenant)
    form = ProspectingSearchPlanForm(request.POST or None, project_queryset=project_queryset)
    if request.method != "POST":
        return _render_search_run_create(request, access=access, form=form)
    _ensure_posted_tenant_matches(access, request)
    if not form.is_valid():
        messages.error(request, "Revise os campos destacados antes de gerar o plano.")
        return _render_search_run_create(request, access=access, form=form)
    project = form.cleaned_data["project"]
    try:
        plan = build_search_plan_for_manual_run(
            tenant=access.tenant,
            project=project,
            objective=form.cleaned_data["objective"],
            target_region=form.cleaned_data["target_region"],
            actor=request.user,
            request=request,
        )
    except (ValidationError, ProspectingSearchRunError) as exc:
        messages.error(request, "; ".join(exc.messages))
        return _render_search_run_create(request, access=access, form=form)
    draft_payload = {
        "tenant_id": str(access.tenant.pk),
        "project_id": str(project.pk),
        "objective": form.cleaned_data["objective"],
        "target_region": form.cleaned_data["target_region"],
        "queries": plan["queries"],
    }
    draft_id = _save_draft(request, draft_payload)
    review_form = ProspectingSearchQueryReviewForm(
        initial={"draft_id": draft_id, "selected_queries": plan["queries"]},
        query_choices=[(query, query) for query in plan["queries"]],
    )
    messages.success(request, "Plano de pesquisa gerado. Revise as queries e execute quando estiver pronto.")
    return _render_search_run_create(
        request,
        access=access,
        form=form,
        draft=draft_payload | {"draft_id": draft_id},
        review_form=review_form,
    )


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_search_run_execute(request):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    draft_id = str(request.POST.get("draft_id") or "").strip()
    draft = _drafts(request).get(draft_id)
    if not draft:
        previous_run_id = _executed_drafts(request).get(draft_id)
        if previous_run_id:
            messages.info(request, "Pesquisa já executada anteriormente para este plano.")
            return redirect("operations_portal:prospecting_search_run_detail", run_id=previous_run_id)
        messages.error(request, "Plano expirado ou inválido. Gere um novo plano de pesquisa.")
        return redirect("operations_portal:prospecting_search_run_create")
    query_choices = [(query, query) for query in draft.get("queries", [])]
    review_form = ProspectingSearchQueryReviewForm(request.POST, query_choices=query_choices)
    form = ProspectingSearchPlanForm(
        initial={
            "project": draft.get("project_id"),
            "objective": draft.get("objective"),
            "target_region": draft.get("target_region"),
        },
        project_queryset=_project_queryset_for_tenant(access.tenant),
    )
    if not review_form.is_valid():
        messages.error(request, "Selecione pelo menos uma query válida para executar.")
        return _render_search_run_create(
            request,
            access=access,
            form=form,
            draft=draft | {"draft_id": draft_id},
            review_form=review_form,
        )
    project = get_object_or_404(Project, pk=draft.get("project_id"), tenant=access.tenant, is_active=True)
    try:
        search_run = create_and_dispatch_search_run(
            tenant=access.tenant,
            project=project,
            objective=draft.get("objective") or "",
            target_region=draft.get("target_region") or "",
            selected_queries=review_form.cleaned_data["selected_queries"],
            actor=request.user,
            request=request,
        )
    except (ValidationError, ProspectingSearchRunError) as exc:
        messages.error(request, "; ".join(exc.messages))
        return _render_search_run_create(
            request,
            access=access,
            form=form,
            draft=draft | {"draft_id": draft_id},
            review_form=review_form,
        )
    try:
        _mark_draft_executed(request, draft_id=draft_id, search_run_id=search_run.id)
    except Exception:
        logger.exception(
            "prospecting_search_run_execute_post_dispatch_failed",
            extra={"search_run_id": str(search_run.id), "draft_id": str(draft_id)},
        )
        messages.warning(request, "Pesquisa criada e enviada, mas não foi possível marcar o plano como executado.")
    else:
        messages.success(request, "Pesquisa criada e enviada para execução.")
    return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)


@login_required(login_url="/admin/login/")
def prospecting_search_run_detail(request, run_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    search_run = get_object_or_404(_search_run_queryset(access.tenant), pk=run_id)
    search_run = _sync_search_run_for_display(search_run)
    _enrich_run_ui(search_run)
    results_qs = _build_search_result_queryset(search_run)
    filter_form = SearchResultFilterForm(request.GET or None)
    if filter_form.is_valid():
        status = filter_form.cleaned_data.get("status") or ""
        phone = filter_form.cleaned_data.get("phone") or ""
        website = filter_form.cleaned_data.get("website") or ""
        query = (filter_form.cleaned_data.get("q") or "").strip()
        if status == "unreviewed":
            results_qs = results_qs.filter(review_status=SearchResult.ReviewStatus.UNREVIEWED, is_promoted=False)
        elif status == "promoted":
            results_qs = results_qs.filter(is_promoted=True)
        elif status == "ignored":
            results_qs = results_qs.filter(review_status=SearchResult.ReviewStatus.IGNORED)
        if phone == "with":
            results_qs = results_qs.exclude(phone__isnull=True).exclude(phone__exact="")
        elif phone == "without":
            results_qs = results_qs.filter(Q(phone__isnull=True) | Q(phone__exact=""))
        if website == "with":
            results_qs = results_qs.exclude(website__isnull=True).exclude(website__exact="")
        elif website == "without":
            results_qs = results_qs.filter(Q(website__isnull=True) | Q(website__exact=""))
        if query:
            results_qs = results_qs.filter(
                Q(name__icontains=query) | Q(address__icontains=query) | Q(phone__icontains=query)
            )
    page_obj = Paginator(results_qs.order_by("name"), 25).get_page(request.GET.get("page") or 1)
    page_ids = list(page_obj.object_list.values_list("id", flat=True))
    source_map = {
        source.search_result_id: source
        for source in ProspectSource.objects.filter(search_result_id__in=page_ids).select_related("prospect")
    }
    results = list(page_obj.object_list)
    promoted_prospect_ids = [source.prospect_id for source in source_map.values()]
    email_enriched_ids = set(
        ProspectEnrichment.objects.filter(
            prospect_id__in=promoted_prospect_ids,
            field=ProspectEnrichment.Field.EMAIL,
        ).values_list("prospect_id", flat=True)
    )
    phone_enriched_ids = set(
        ProspectEnrichment.objects.filter(
            prospect_id__in=promoted_prospect_ids,
            field=ProspectEnrichment.Field.PHONE,
        ).values_list("prospect_id", flat=True)
    )
    for result in results:
        result.promoted_source = source_map.get(result.id)
        result.review_status_label = SearchResult.ReviewStatus(result.review_status).label
        if result.promoted_source:
            pid = result.promoted_source.prospect_id
            result.has_discovered_email = pid in email_enriched_ids
            result.has_discovered_phone = pid in phone_enriched_ids
        else:
            result.has_discovered_email = False
            result.has_discovered_phone = False

    promoted_count = ProspectSource.objects.filter(search_result__search_run=search_run).count()
    ignored_count = search_run.results.filter(review_status=SearchResult.ReviewStatus.IGNORED).count()
    total_results = search_run.results.count()
    unreviewed_count = max(total_results - promoted_count - ignored_count, 0)
    attempts = list(_ensure_attempt_tracking(search_run))
    current_attempt = attempts[-1] if attempts else None
    execution = current_attempt.tool_execution if current_attempt else None
    execution_summary = summarize_tool_execution(execution) if execution is not None else None
    execution_error_message = _execution_error_message(execution)
    execution_metadata = _execution_metadata_view(execution)
    attempts_view = []
    for attempt in attempts:
        attempt_execution = attempt.tool_execution
        payload = attempt_execution.result_payload if isinstance(attempt_execution.result_payload, dict) else {}
        stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        attempts_view.append(
            {
                "attempt_number": attempt.attempt_number,
                "tool_execution_id": attempt_execution.id,
                "status": attempt_execution.status,
                "status_label": _attempt_status_label(attempt_execution.status),
                "executor_name": attempt_execution.executor.name if attempt_execution.executor else "Aguardando executor",
                "created_at": attempt_execution.created_at,
                "started_at": attempt_execution.started_at,
                "completed_at": attempt_execution.completed_at,
                "error_code": attempt_execution.error_code,
                "error_message": _execution_error_message(attempt_execution),
                "result_count": stats.get("businesses_returned"),
                "is_current": current_attempt and attempt.id == current_attempt.id,
            }
        )

    compatible_executors = _compatible_executor_snapshot(search_run, execution)
    has_online_executor = any(item["is_online"] for item in compatible_executors)
    can_manage = access.is_global or CAPABILITY_COMMERCIAL_MANAGE in access.capabilities
    waiting_executor = execution is not None and execution.status == ToolExecution.Status.DISPATCHED
    show_offline_warning = waiting_executor and bool(compatible_executors) and not has_online_executor
    show_no_executor_warning = waiting_executor and not compatible_executors
    can_retry = bool(
        execution
        and execution.status in {ToolExecution.Status.FAILED, ToolExecution.Status.EXPIRED, ToolExecution.Status.CANCELLED}
        and can_manage
    )
    can_cancel = bool(
        execution
        and execution.status in {ToolExecution.Status.PENDING, ToolExecution.Status.DISPATCHED}
        and can_manage
    )
    can_redispatch = bool(
        execution
        and execution.status in {ToolExecution.Status.PENDING, ToolExecution.Status.DISPATCHED}
        and can_manage
    )
    execution_is_expired = bool(execution and execution.status == ToolExecution.Status.EXPIRED)
    execution_waiting_claim = bool(execution and execution.status == ToolExecution.Status.DISPATCHED and execution.executor_id is None)

    context = {
        "active_section": "prospeccao",
        "search_run": search_run,
        "results": results,
        "page_obj": page_obj,
        "querystring": clean_querystring(request.GET),
        "filter_form": filter_form,
        "execution": execution,
        "execution_summary": execution_summary,
        "execution_error_message": execution_error_message,
        "execution_progress": execution_metadata["progress"],
        "execution_diagnostics": execution_metadata["diagnostics"],
        "attempts": attempts_view,
        "current_attempt_number": current_attempt.attempt_number if current_attempt else None,
        "compatible_executors": compatible_executors,
        "show_offline_warning": show_offline_warning,
        "show_no_executor_warning": show_no_executor_warning,
        "can_retry": can_retry,
        "can_cancel": can_cancel,
        "can_redispatch": can_redispatch,
        "execution_is_expired": execution_is_expired,
        "execution_waiting_claim": execution_waiting_claim,
        "total_results": total_results,
        "unreviewed_count": unreviewed_count,
        "promoted_count": promoted_count,
        "ignored_count": ignored_count,
        "can_manage": can_manage,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/search_run_detail.html", context)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_search_run_refresh(request, run_id):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    search_run = get_object_or_404(_search_run_queryset(access.tenant), pk=run_id)
    try:
        synchronized = synchronize_search_run(search_run=search_run)
    except (ValidationError, ProspectingSearchRunError) as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)
    ui = _search_run_ui(synchronized.status)
    messages.info(request, f"Status atualizado: {ui['label']}.")
    return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_search_run_redispatch(request, run_id):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    search_run = get_object_or_404(_search_run_queryset(access.tenant), pk=run_id)
    try:
        outcome = redispatch_search_run(search_run=search_run, actor=request.user, request=request)
        synchronized = synchronize_search_run(search_run=outcome.search_run)
    except (ValidationError, ProspectingSearchRunError, ProspectingExecutionRecoveryError) as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)
    if outcome.transitioned_to_dispatched:
        messages.success(request, "Execução redisparada para a fila do executor.")
    else:
        messages.info(request, "A execução atual já estava aguardando executor. Nenhuma tentativa duplicada foi criada.")
    ui = _search_run_ui(synchronized.status)
    messages.info(request, f"Status atualizado: {ui['label']}.")
    return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_search_run_retry(request, run_id):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    search_run = get_object_or_404(_search_run_queryset(access.tenant), pk=run_id)
    try:
        outcome = retry_search_run(search_run=search_run, actor=request.user, request=request)
        synchronized = synchronize_search_run(search_run=outcome.search_run)
    except (ValidationError, ProspectingSearchRunError, ProspectingExecutionRecoveryError) as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)
    if outcome.created_new_attempt:
        messages.success(request, f"Nova tentativa {outcome.attempt.attempt_number} enviada ao executor.")
    else:
        messages.info(request, "A tentativa atual ainda está em andamento. Nenhuma nova tentativa foi criada.")
    ui = _search_run_ui(synchronized.status)
    messages.info(request, f"Status atualizado: {ui['label']}.")
    return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_search_run_cancel(request, run_id):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    search_run = get_object_or_404(_search_run_queryset(access.tenant), pk=run_id)
    try:
        cancelled = cancel_search_run(search_run=search_run, actor=request.user, request=request)
    except (ValidationError, ProspectingExecutionRecoveryError) as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)
    messages.success(request, "Pesquisa cancelada. O histórico e os resultados já existentes foram preservados.")
    ui = _search_run_ui(cancelled.status)
    messages.info(request, f"Status atualizado: {ui['label']}.")
    return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_promote_search_result(request, result_id):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    search_result = get_object_or_404(_search_result_queryset(access.tenant), pk=result_id)
    try:
        if search_result.review_status == SearchResult.ReviewStatus.IGNORED:
            restore_search_result_to_unreviewed(tenant=search_result.tenant, search_result=search_result)
        prospect = promote_search_result_to_prospect(tenant=search_result.tenant, search_result=search_result)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_search_run_detail", run_id=search_result.search_run_id)

    record_audit_event(
        action=ACTION_PROSPECT_PROMOTED,
        actor=request.user,
        tenant=search_result.tenant,
        obj=prospect,
        metadata={
            "prospect_id": str(prospect.id),
            "search_result_id": str(search_result.id),
            "search_run_id": str(search_result.search_run_id),
        },
        request=request,
    )
    messages.success(request, "SearchResult promovido para Prospect.")
    return redirect("operations_portal:prospecting_search_run_detail", run_id=search_result.search_run_id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_promote_and_enrich_search_result(request, result_id):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    search_result = get_object_or_404(_search_result_queryset(access.tenant), pk=result_id)
    prospect = None
    try:
        if search_result.review_status == SearchResult.ReviewStatus.IGNORED:
            restore_search_result_to_unreviewed(tenant=search_result.tenant, search_result=search_result)
        prospect = promote_search_result_to_prospect(tenant=search_result.tenant, search_result=search_result)
        enrichment_result = enrich_prospect_from_website(tenant=prospect.tenant, prospect=prospect)
    except ValidationError as exc:
        messages.error(request, website_enrichment_error_message(exc))
        if prospect is not None:
            return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
        return redirect("operations_portal:prospecting_search_run_detail", run_id=search_result.search_run_id)

    record_audit_event(
        action=ACTION_PROSPECT_PROMOTED,
        actor=request.user,
        tenant=search_result.tenant,
        obj=prospect,
        metadata={
            "prospect_id": str(prospect.id),
            "search_result_id": str(search_result.id),
            "search_run_id": str(search_result.search_run_id),
            "promote_and_enrich": True,
        },
        request=request,
    )
    record_audit_event(
        action=ACTION_PROSPECT_WEBSITE_ENRICHED,
        actor=request.user,
        tenant=prospect.tenant,
        obj=prospect,
        metadata={
            "prospect_id": str(prospect.id),
            "pages_fetched": enrichment_result.pages_fetched,
            "emails_found": enrichment_result.emails_found,
            "phones_found": enrichment_result.phones_found,
            "addresses_found": enrichment_result.addresses_found,
        },
        request=request,
    )
    messages.success(
        request,
        (
            f"Prospect promovido e website enriquecido. Emails: {enrichment_result.emails_found}, "
            f"telefones: {enrichment_result.phones_found}, endereços: {enrichment_result.addresses_found}."
        ),
    )
    for warning in enrichment_result.warnings[:3]:
        messages.warning(request, warning)
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_ignore_search_result(request, result_id):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    search_result = get_object_or_404(_search_result_queryset(access.tenant), pk=result_id)
    try:
        mark_search_result_ignored(tenant=search_result.tenant, search_result=search_result)
    except (ValidationError, ProspectingReviewError) as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "SearchResult marcado como ignorado.")
    return redirect("operations_portal:prospecting_search_run_detail", run_id=search_result.search_run_id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_restore_search_result(request, result_id):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    search_result = get_object_or_404(_search_result_queryset(access.tenant), pk=result_id)
    try:
        restore_search_result_to_unreviewed(tenant=search_result.tenant, search_result=search_result)
    except (ValidationError, ProspectingReviewError) as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "SearchResult voltou para não revisado.")
    return redirect("operations_portal:prospecting_search_run_detail", run_id=search_result.search_run_id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_search_result_bulk_action(request, run_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=False, require_tenant=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    search_run = get_object_or_404(_search_run_queryset(access.tenant), pk=run_id)
    selected_ids, scoped_queryset = _resolve_search_result_action_targets(access=access, request=request, search_run=search_run)
    form = SearchResultBulkActionForm(request.POST, result_choices=[(str(pk), str(pk)) for pk in selected_ids])
    if not form.is_valid():
        messages.error(request, "Selecione ao menos um SearchResult válido para executar a ação em lote.")
        return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)
    if scoped_queryset.count() != len(set(selected_ids)):
        messages.error(request, "Foram detectados itens inválidos para o tenant ou para a pesquisa selecionada.")
        return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)
    selected_results = list(scoped_queryset)
    action = form.cleaned_data["action"]
    if action == "promote":
        summary = bulk_promote_search_results(tenant=search_run.tenant, search_results=selected_results)
        messages.success(
            request,
            (
                f"Ação concluída: {summary.promoted} promovidos, "
                f"{summary.already_promoted} já promovidos, {summary.failed} falharam."
            ),
        )
    else:
        summary = bulk_ignore_search_results(tenant=search_run.tenant, search_results=selected_results)
        messages.success(
            request,
            (
                f"Ação concluída: {summary.ignored} ignorados, {summary.already_ignored} já ignorados, "
                f"{summary.promoted_conflict} em conflito por já estarem promovidos, {summary.failed} falharam."
            ),
        )
    return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)


@login_required(login_url="/admin/login/")
def prospecting_prospect_list(request):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    pending_follow_up_qs = ProspectFollowUp.objects.filter(
        prospect_id=OuterRef("pk"),
        status=ProspectFollowUp.Status.PENDING,
    )
    if access.tenant is not None:
        pending_follow_up_qs = pending_follow_up_qs.filter(tenant=access.tenant)
    email_enrichment_qs = ProspectEnrichment.objects.filter(
        prospect_id=OuterRef("pk"),
        field=ProspectEnrichment.Field.EMAIL,
    )
    phone_enrichment_qs = ProspectEnrichment.objects.filter(
        prospect_id=OuterRef("pk"),
        field=ProspectEnrichment.Field.PHONE,
    )
    if access.tenant is not None:
        email_enrichment_qs = email_enrichment_qs.filter(tenant=access.tenant)
        phone_enrichment_qs = phone_enrichment_qs.filter(tenant=access.tenant)
    queryset = _prospect_queryset(access.tenant).annotate(
        contact_count=Count("contacts", distinct=True),
        activity_count=Count("activities", distinct=True),
        enrichment_count=Count("enrichments", distinct=True),
        last_activity_at=Max("activities__occurred_at"),
        has_discovered_email=Exists(email_enrichment_qs),
        has_discovered_phone=Exists(phone_enrichment_qs),
        next_follow_up_due_at=Subquery(pending_follow_up_qs.order_by("due_at").values("due_at")[:1]),
        next_follow_up_action_type=Subquery(pending_follow_up_qs.order_by("due_at").values("action_type")[:1]),
        next_follow_up_overdue=Exists(
            pending_follow_up_qs.filter(due_at__lt=timezone.now(), due_at__isnull=False)
        ),
    )
    form = ProspectFilterForm(request.GET or None, project_queryset=_project_queryset_for_tenant(access.tenant))
    if form.is_valid():
        project = form.cleaned_data.get("project")
        has_contacts = form.cleaned_data.get("has_contacts")
        discovered_email = form.cleaned_data.get("discovered_email")
        discovered_phone = form.cleaned_data.get("discovered_phone")
        has_activities = form.cleaned_data.get("has_activities")
        query = (form.cleaned_data.get("q") or "").strip()
        if project:
            queryset = queryset.filter(sources__search_run__project=project)
        if has_contacts == "yes":
            queryset = queryset.filter(contact_count__gt=0)
        elif has_contacts == "no":
            queryset = queryset.filter(contact_count=0)
        if discovered_email == "yes":
            queryset = queryset.filter(has_discovered_email=True)
        elif discovered_email == "no":
            queryset = queryset.filter(has_discovered_email=False)
        if discovered_phone == "yes":
            queryset = queryset.filter(has_discovered_phone=True)
        elif discovered_phone == "no":
            queryset = queryset.filter(has_discovered_phone=False)
        if has_activities == "yes":
            queryset = queryset.filter(activity_count__gt=0)
        elif has_activities == "no":
            queryset = queryset.filter(activity_count=0)
        qualification_status = form.cleaned_data.get("qualification_status")
        priority = form.cleaned_data.get("priority")
        if qualification_status:
            queryset = queryset.filter(qualification_status=qualification_status)
        if priority:
            queryset = queryset.filter(priority=priority)
        follow_up = form.cleaned_data.get("follow_up")
        if follow_up == "with":
            queryset = queryset.filter(
                follow_ups__status=ProspectFollowUp.Status.PENDING,
            ).distinct()
        elif follow_up == "without":
            queryset = queryset.exclude(follow_ups__status=ProspectFollowUp.Status.PENDING)
        elif follow_up == "overdue":
            queryset = queryset.filter(
                follow_ups__status=ProspectFollowUp.Status.PENDING,
                follow_ups__due_at__lt=timezone.now(),
            ).distinct()
        outcome = form.cleaned_data.get("outcome")
        if outcome:
            queryset = queryset.filter(contact_outcomes__outcome=outcome).distinct()
        if query:
            queryset = queryset.filter(
                Q(display_name__icontains=query)
                | Q(address__icontains=query)
                | Q(phone__icontains=query)
                | Q(website__icontains=query)
                | Q(maps_url__icontains=query)
                | Q(external_id__icontains=query)
            )
    page_obj = Paginator(queryset.distinct().order_by("display_name"), 25).get_page(request.GET.get("page") or 1)
    context = {
        "active_section": "prospeccao",
        "form": form,
        "page_obj": page_obj,
        "querystring": clean_querystring(request.GET),
        "can_manage": access.is_global or CAPABILITY_COMMERCIAL_MANAGE in access.capabilities,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/prospect_list.html", context)


@login_required(login_url="/admin/login/")
def prospecting_follow_up_queue(request):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    tenant, tenant_ids = _follow_up_queue_tenant_scope(access)
    form = FollowUpQueueFilterForm(request.GET or None)
    queryset = base_follow_up_queue_queryset(tenant=tenant, tenant_ids=tenant_ids)
    if form.is_valid():
        queryset = apply_follow_up_queue_filters(
            queryset,
            situation=form.cleaned_data.get("situation") or "pending",
            action_type=form.cleaned_data.get("action_type") or "",
            prospect_query=form.cleaned_data.get("prospect_q") or "",
            contact_query=form.cleaned_data.get("contact_q") or "",
            priority=form.cleaned_data.get("priority") or "",
            period=form.cleaned_data.get("period") or "",
        )
    else:
        queryset = queryset.filter(status=ProspectFollowUp.Status.PENDING)
    queryset = order_follow_up_queue(annotate_follow_up_sort_bucket(queryset))
    page_obj = Paginator(queryset, 25).get_page(request.GET.get("page") or 1)
    outcome_label_map = dict(ProspectContactOutcome.Outcome.choices)
    for item in page_obj.object_list:
        item.temporal_label = classify_follow_up_temporal(item)
        item.latest_outcome_display = outcome_label_map.get(item.latest_outcome, "") if item.latest_outcome else ""
    counters = compute_follow_up_queue_counters(tenant=tenant, tenant_ids=tenant_ids)
    context = {
        "active_section": "prospeccao",
        "active_prospecting_page": "acompanhamentos",
        "form": form,
        "page_obj": page_obj,
        "querystring": clean_querystring(request.GET),
        "counters": counters,
        "can_manage": _can_mutate_follow_up_queue(access),
        "show_tenant_column": access.is_global or access.show_all_tenants,
        "global_read_only": access.is_global or access.tenant is None,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/follow_up_queue.html", context)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_follow_up_queue_complete(request, follow_up_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    if not _can_mutate_follow_up_queue(access):
        raise PermissionDenied
    tenant, tenant_ids = _follow_up_queue_tenant_scope(access)
    follow_up = get_object_or_404(base_follow_up_queue_queryset(tenant=tenant, tenant_ids=tenant_ids), pk=follow_up_id)
    try:
        complete_prospect_follow_up(tenant=follow_up.tenant, follow_up=follow_up, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(_follow_up_queue_redirect(request))
    messages.success(request, "Próxima ação concluída.")
    return redirect(_follow_up_queue_redirect(request))


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_follow_up_queue_cancel(request, follow_up_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    if not _can_mutate_follow_up_queue(access):
        raise PermissionDenied
    tenant, tenant_ids = _follow_up_queue_tenant_scope(access)
    follow_up = get_object_or_404(base_follow_up_queue_queryset(tenant=tenant, tenant_ids=tenant_ids), pk=follow_up_id)
    try:
        cancel_prospect_follow_up(tenant=follow_up.tenant, follow_up=follow_up, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(_follow_up_queue_redirect(request))
    messages.success(request, "Próxima ação cancelada.")
    return redirect(_follow_up_queue_redirect(request))


@login_required(login_url="/admin/login/")
def prospecting_my_queue(request):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    tenant, tenant_ids = _follow_up_queue_tenant_scope(access)
    tenant_names = {item.pk: item.name for item in access.accessible_tenants}
    has_scope = tenant is not None or bool(tenant_ids)
    items = build_commercial_queue(tenant=tenant, tenant_ids=tenant_ids, compact=True) if has_scope else []
    counters = commercial_queue_counters_from_items(items) if has_scope else CommercialQueueCounters(0, 0, 0, 0, 0, 0)
    form = MyQueueFilterForm(request.GET or None)
    if form.is_valid():
        items = _apply_my_queue_filters(
            items,
            category=form.cleaned_data.get("category") or "",
            priority=form.cleaned_data.get("priority") or "",
            prospect_q=form.cleaned_data.get("prospect_q") or "",
        )
    priority_labels = dict(Prospect.Priority.choices)
    rows = _build_my_queue_rows(items=items, tenant=tenant, tenant_ids=tenant_ids, tenant_names=tenant_names)
    for row in rows:
        row["cta_url"] = _my_queue_primary_cta_url(item=row["item"], waiting_send_id=row["waiting_send_id"])
        row["cta_label"] = _my_queue_primary_cta_label(category=row["item"].category, item=row["item"])
        priority = row["item"].priority
        row["priority_display"] = "—" if priority == Prospect.Priority.UNSET else priority_labels.get(priority, priority)
    page_obj = Paginator(rows, 25).get_page(request.GET.get("page") or 1)
    context = {
        "active_section": "prospeccao",
        "active_prospecting_page": "minha_fila",
        "form": form,
        "page_obj": page_obj,
        "querystring": clean_querystring(request.GET),
        "counters": counters,
        "can_manage": _can_mutate_follow_up_queue(access),
        "show_tenant_column": access.is_global or access.show_all_tenants,
        "global_read_only": access.is_global or access.tenant is None,
        "return_view": "prospecting_my_queue",
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/my_queue.html", context)


@login_required(login_url="/admin/login/")
def prospecting_prospect_detail(request, prospect_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    prospect = get_object_or_404(
        _prospect_queryset(access.tenant).prefetch_related(
            "sources__search_result",
            "sources__search_run",
            "sources__search_run__project",
            "enrichments",
            "enrichments__source_search_result",
        ),
        pk=prospect_id,
    )
    contacts = list(prospect.contacts.all())
    editing_contact = None
    edit_contact_id = (request.GET.get("edit_contact") or "").strip()
    if edit_contact_id:
        editing_contact = next((item for item in contacts if str(item.pk) == edit_contact_id), None)
        if editing_contact is None:
            editing_contact = get_object_or_404(_contact_queryset(access.tenant), pk=edit_contact_id, prospect=prospect)
    activities = list(
        _activity_queryset(access.tenant)
        .filter(prospect=prospect)
        .order_by("-occurred_at", "-created_at")
    )
    editing_activity = None
    edit_activity_id = (request.GET.get("edit_activity") or "").strip()
    if edit_activity_id:
        editing_activity = next((item for item in activities if str(item.pk) == edit_activity_id), None)
        if editing_activity is None:
            editing_activity = get_object_or_404(_activity_queryset(access.tenant), pk=edit_activity_id, prospect=prospect)
    outreach_qs = _outreach_draft_queryset(access.tenant).filter(prospect=prospect)
    outreach_status = (request.GET.get("outreach_status") or "").strip().upper()
    if outreach_status in ProspectOutreachDraft.Status.values:
        outreach_qs = outreach_qs.filter(status=outreach_status)
    outreach_drafts = list(outreach_qs.order_by("-updated_at", "-created_at"))
    editing_outreach = None
    edit_outreach_id = (request.GET.get("edit_outreach") or "").strip()
    if edit_outreach_id:
        editing_outreach = next((item for item in outreach_drafts if str(item.pk) == edit_outreach_id), None)
        if editing_outreach is None:
            editing_outreach = get_object_or_404(_outreach_draft_queryset(access.tenant), pk=edit_outreach_id, prospect=prospect)
    pending_follow_ups = list(
        _follow_up_queryset(access.tenant)
        .filter(prospect=prospect, status=ProspectFollowUp.Status.PENDING)
        .order_by("due_at", "-created_at")
    )
    record_outcome_send = None
    record_outcome_send_id = (request.GET.get("record_outcome_send") or "").strip()
    if record_outcome_send_id:
        record_outcome_send = get_object_or_404(
            _outreach_send_queryset(access.tenant),
            pk=record_outcome_send_id,
            prospect=prospect,
            status=ProspectOutreachSend.Status.SENT,
        )
    show_record_outcome = (request.GET.get("record_outcome") or "").strip() == "1" or record_outcome_send is not None
    enrichment_rows = list(prospect.enrichments.all().order_by("-observed_at", "-created_at"))
    enrichment_summary = {
        "email": sum(1 for item in enrichment_rows if item.field == ProspectEnrichment.Field.EMAIL),
        "phone": sum(1 for item in enrichment_rows if item.field == ProspectEnrichment.Field.PHONE),
        "address": sum(1 for item in enrichment_rows if item.field == ProspectEnrichment.Field.ADDRESS),
    }
    contact_create_initial = {}
    create_from_enrichment_id = (request.GET.get("create_contact_from") or "").strip()
    if create_from_enrichment_id:
        source_enrichment = get_object_or_404(
            ProspectEnrichment.objects.filter(prospect=prospect, tenant=prospect.tenant),
            pk=create_from_enrichment_id,
        )
        if source_enrichment.field == ProspectEnrichment.Field.EMAIL:
            contact_create_initial["email"] = source_enrichment.value
        elif source_enrichment.field == ProspectEnrichment.Field.PHONE:
            contact_create_initial["phone"] = source_enrichment.value
    context = {
        "active_section": "prospeccao",
        "prospect": prospect,
        "contacts": contacts,
        "activities": activities,
        "outreach_drafts": outreach_drafts,
        "outreach_status_filter": outreach_status,
        "editing_contact": editing_contact,
        "editing_activity": editing_activity,
        "editing_outreach": editing_outreach,
        "contact_create_form": ProspectContactForm(initial=contact_create_initial),
        "enrichment_summary": enrichment_summary,
        "contact_edit_form": ProspectContactForm(contact=editing_contact) if editing_contact else None,
        "activity_create_form": ProspectActivityForm(prospect=prospect),
        "activity_edit_form": ProspectActivityForm(prospect=prospect, activity=editing_activity) if editing_activity else None,
        "outreach_create_form": ProspectOutreachDraftForm(prospect=prospect),
        "outreach_edit_form": ProspectOutreachDraftForm(prospect=prospect, draft=editing_outreach) if editing_outreach else None,
        "can_create_outreach": prospect.qualification_status == Prospect.QualificationStatus.QUALIFIED,
        "show_new_outreach": (request.GET.get("new_outreach") or "").strip() == "1",
        "viewing_outreach_send": _resolve_viewing_outreach_send(access.tenant, prospect, request.GET.get("view_outreach_send")),
        "pending_follow_ups": pending_follow_ups,
        "show_record_outcome": show_record_outcome,
        "outcome_form": ProspectContactOutcomeForm(prospect=prospect, initial_outreach_send=record_outcome_send),
        "follow_up_create_form": ProspectFollowUpForm(prospect=prospect),
        "show_new_follow_up": (request.GET.get("new_follow_up") or "").strip() == "1",
        "now": timezone.now(),
        "sources": list(prospect.sources.all().order_by("-created_at")),
        "enrichments": enrichment_rows,
        "manual_form": ProspectEnrichmentCreateForm(
            initial={
                "field": ProspectEnrichment.Field.EMAIL,
                "source_type": ProspectEnrichment.SourceType.MANUAL,
            }
        ),
        "qualification_form": ProspectQualificationForm(prospect=prospect),
        "can_manage": access.is_global or CAPABILITY_COMMERCIAL_MANAGE in access.capabilities,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/prospect_detail.html", context)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_qualify_prospect(request, prospect_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    form = ProspectQualificationForm(request.POST, prospect=prospect)
    if not form.is_valid():
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
    try:
        _, changed = qualify_prospect(
            tenant=prospect.tenant,
            prospect=prospect,
            qualification_status=form.cleaned_data["qualification_status"],
            priority=form.cleaned_data["priority"],
            qualification_note=form.cleaned_data.get("qualification_note") or "",
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)

    if changed:
        messages.success(request, "Qualificação salva.")
    else:
        messages.info(request, "Qualificação já estava atualizada.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_create_prospect_contact(request, prospect_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    form = ProspectContactForm(request.POST)
    if not form.is_valid():
        for error in form.non_field_errors():
            messages.error(request, error)
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
    try:
        create_prospect_contact(
            tenant=prospect.tenant,
            prospect=prospect,
            name=form.cleaned_data.get("name") or "",
            role_title=form.cleaned_data.get("role_title") or "",
            email=form.cleaned_data.get("email") or "",
            phone=form.cleaned_data.get("phone") or "",
            note=form.cleaned_data.get("note") or "",
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
    messages.success(request, "Contato adicionado.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_update_prospect_contact(request, prospect_id, contact_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    contact = get_object_or_404(_contact_queryset(access.tenant), pk=contact_id, prospect=prospect)
    form = ProspectContactForm(request.POST, contact=contact)
    if not form.is_valid():
        for error in form.non_field_errors():
            messages.error(request, error)
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?edit_contact={contact.id}")
    try:
        _, changed = update_prospect_contact(
            tenant=prospect.tenant,
            contact=contact,
            name=form.cleaned_data.get("name") or "",
            role_title=form.cleaned_data.get("role_title") or "",
            email=form.cleaned_data.get("email") or "",
            phone=form.cleaned_data.get("phone") or "",
            note=form.cleaned_data.get("note") or "",
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?edit_contact={contact.id}")
    messages.success(request, "Contato atualizado." if changed else "Contato já estava atualizado.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_delete_prospect_contact(request, prospect_id, contact_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    contact = get_object_or_404(_contact_queryset(access.tenant), pk=contact_id, prospect=prospect)
    try:
        delete_prospect_contact(tenant=prospect.tenant, contact=contact, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
    messages.success(request, "Contato removido.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_create_prospect_activity(request, prospect_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    form = ProspectActivityForm(request.POST, prospect=prospect)
    if not form.is_valid():
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
    contact = form.cleaned_data.get("contact")
    if contact is not None and contact.prospect_id != prospect.id:
        raise PermissionDenied
    try:
        create_prospect_activity(
            tenant=prospect.tenant,
            prospect=prospect,
            activity_type=form.cleaned_data["activity_type"],
            note=form.cleaned_data["note"],
            occurred_at=form.cleaned_data["occurred_at"],
            contact=contact,
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
    messages.success(request, "Atividade registrada.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_update_prospect_activity(request, prospect_id, activity_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    activity = get_object_or_404(_activity_queryset(access.tenant), pk=activity_id, prospect=prospect)
    form = ProspectActivityForm(request.POST, prospect=prospect, activity=activity)
    if not form.is_valid():
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?edit_activity={activity.id}")
    contact = form.cleaned_data.get("contact")
    if contact is not None and contact.prospect_id != prospect.id:
        raise PermissionDenied
    try:
        _, changed = update_prospect_activity(
            tenant=prospect.tenant,
            activity=activity,
            activity_type=form.cleaned_data["activity_type"],
            note=form.cleaned_data["note"],
            occurred_at=form.cleaned_data["occurred_at"],
            contact=contact,
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?edit_activity={activity.id}")
    messages.success(request, "Atividade atualizada." if changed else "Atividade já estava atualizada.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_delete_prospect_activity(request, prospect_id, activity_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    activity = get_object_or_404(_activity_queryset(access.tenant), pk=activity_id, prospect=prospect)
    try:
        delete_prospect_activity(tenant=prospect.tenant, activity=activity, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
    messages.success(request, "Atividade removida.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


def _outreach_draft_redirect(*, prospect, draft_id=None, outreach_status=""):
    url = reverse("operations_portal:prospecting_prospect_detail", args=[prospect.id])
    params = {}
    if draft_id:
        params["edit_outreach"] = str(draft_id)
    if outreach_status:
        params["outreach_status"] = outreach_status
    if params:
        return f"{url}?{urlencode(params)}"
    return url


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_create_outreach_draft(request, prospect_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    form = ProspectOutreachDraftForm(request.POST, prospect=prospect)
    if not form.is_valid():
        for error in form.non_field_errors():
            messages.error(request, error)
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?new_outreach=1")
    contact = form.cleaned_data["contact"]
    if contact.prospect_id != prospect.id:
        raise PermissionDenied
    save_intent = (request.POST.get("save_intent") or "draft").strip().lower()
    status = ProspectOutreachDraft.Status.READY if save_intent == "ready" else ProspectOutreachDraft.Status.DRAFT
    try:
        create_outreach_draft(
            tenant=prospect.tenant,
            prospect=prospect,
            contact=contact,
            channel=form.cleaned_data["channel"],
            subject=form.cleaned_data.get("subject") or "",
            body=form.cleaned_data["body"],
            status=status,
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?new_outreach=1")
    messages.success(request, "Abordagem salva como pronta." if status == ProspectOutreachDraft.Status.READY else "Rascunho de abordagem salvo.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_update_outreach_draft(request, prospect_id, draft_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    draft = get_object_or_404(_outreach_draft_queryset(access.tenant), pk=draft_id, prospect=prospect)
    form = ProspectOutreachDraftForm(request.POST, prospect=prospect, draft=draft)
    if not form.is_valid():
        for error in form.non_field_errors():
            messages.error(request, error)
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect(_outreach_draft_redirect(prospect=prospect, draft_id=draft.id))
    contact = form.cleaned_data["contact"]
    if contact.prospect_id != prospect.id:
        raise PermissionDenied
    try:
        _, changed = update_outreach_draft(
            tenant=prospect.tenant,
            draft=draft,
            contact=contact,
            channel=form.cleaned_data["channel"],
            subject=form.cleaned_data.get("subject") or "",
            body=form.cleaned_data["body"],
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(_outreach_draft_redirect(prospect=prospect, draft_id=draft.id))
    messages.success(request, "Abordagem atualizada." if changed else "Abordagem já estava atualizada.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_mark_outreach_draft_ready(request, prospect_id, draft_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    draft = get_object_or_404(_outreach_draft_queryset(access.tenant), pk=draft_id, prospect=prospect)
    try:
        mark_outreach_draft_ready(tenant=prospect.tenant, draft=draft, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(_outreach_draft_redirect(prospect=prospect, draft_id=draft.id))
    messages.success(request, "Abordagem marcada como pronta.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_revert_outreach_draft(request, prospect_id, draft_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    draft = get_object_or_404(_outreach_draft_queryset(access.tenant), pk=draft_id, prospect=prospect)
    try:
        revert_outreach_draft_to_draft(tenant=prospect.tenant, draft=draft, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(_outreach_draft_redirect(prospect=prospect, draft_id=draft.id))
    messages.success(request, "Abordagem voltou para rascunho.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_archive_outreach_draft(request, prospect_id, draft_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    draft = get_object_or_404(_outreach_draft_queryset(access.tenant), pk=draft_id, prospect=prospect)
    try:
        archive_outreach_draft(tenant=prospect.tenant, draft=draft, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(_outreach_draft_redirect(prospect=prospect, draft_id=draft.id))
    messages.success(request, "Abordagem arquivada.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
def prospecting_outreach_email_send_confirm(request, prospect_id, draft_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    draft = get_object_or_404(_outreach_draft_queryset(access.tenant), pk=draft_id, prospect=prospect)
    send = getattr(draft, "outreach_send", None)
    context = {
        "active_section": "prospeccao",
        "prospect": prospect,
        "draft": draft,
        "send": send,
        "can_manage": True,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/outreach_email_send_confirm.html", context)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_outreach_email_send_execute(request, prospect_id, draft_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    draft = get_object_or_404(_outreach_draft_queryset(access.tenant), pk=draft_id, prospect=prospect)
    try:
        send, delivered = send_outreach_draft_email(
            tenant=prospect.tenant,
            draft=draft,
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(
            reverse("operations_portal:prospecting_outreach_email_send_confirm", args=[prospect.id, draft.id])
        )
    if not delivered and send.status == ProspectOutreachSend.Status.SENT:
        messages.info(request, "Este e-mail já havia sido enviado.")
    elif send.status == ProspectOutreachSend.Status.SENT:
        messages.success(request, f"E-mail enviado para {send.destination}.")
    else:
        messages.error(request, send.error_message or "Falha ao enviar e-mail.")
    return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?view_outreach_send={send.id}")


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_outreach_email_send_retry(request, prospect_id, send_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    send = get_object_or_404(_outreach_send_queryset(access.tenant), pk=send_id, prospect=prospect)
    try:
        send, _delivered = retry_outreach_send(
            tenant=prospect.tenant,
            send=send,
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?view_outreach_send={send.id}")
    if send.status == ProspectOutreachSend.Status.SENT:
        messages.success(request, f"E-mail enviado para {send.destination}.")
    else:
        messages.error(request, send.error_message or "Falha ao reenviar e-mail.")
    return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?view_outreach_send={send.id}")


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_restore_outreach_draft(request, prospect_id, draft_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    draft = get_object_or_404(_outreach_draft_queryset(access.tenant), pk=draft_id, prospect=prospect)
    try:
        restore_archived_outreach_draft(tenant=prospect.tenant, draft=draft, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(_outreach_draft_redirect(prospect=prospect, draft_id=draft.id))
    messages.success(request, "Abordagem restaurada para rascunho.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_record_contact_outcome(request, prospect_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    form = ProspectContactOutcomeForm(request.POST, prospect=prospect)
    if not form.is_valid():
        for error in form.non_field_errors():
            messages.error(request, error)
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?record_outcome=1")
    contact = form.cleaned_data["contact"]
    outreach_send = form.cleaned_data.get("outreach_send")
    if contact.prospect_id != prospect.id:
        raise PermissionDenied
    if outreach_send is not None and outreach_send.prospect_id != prospect.id:
        raise PermissionDenied
    try:
        follow_up_action = form.cleaned_data.get("follow_up_action_type") if form.cleaned_data.get("create_follow_up") else None
        _, created, follow_up = record_prospect_contact_outcome(
            tenant=prospect.tenant,
            prospect=prospect,
            contact=contact,
            outcome=form.cleaned_data["outcome"],
            note=form.cleaned_data.get("note") or "",
            occurred_at=form.cleaned_data["occurred_at"],
            outreach_send=outreach_send,
            idempotency_key=form.cleaned_data["idempotency_key"],
            actor=request.user,
            request=request,
            follow_up_action_type=follow_up_action,
            follow_up_due_at=form.cleaned_data.get("follow_up_due_at"),
            follow_up_note=form.cleaned_data.get("follow_up_note") or "",
            follow_up_idempotency_key=form.cleaned_data.get("follow_up_idempotency_key") or "",
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?record_outcome=1")
    if created:
        messages.success(request, "Resultado registrado." + (" Próxima ação criada." if follow_up else ""))
    else:
        messages.info(request, "Resultado já havia sido registrado.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_create_follow_up(request, prospect_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    form = ProspectFollowUpForm(request.POST, prospect=prospect)
    if not form.is_valid():
        for error in form.non_field_errors():
            messages.error(request, error)
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?new_follow_up=1")
    contact = form.cleaned_data.get("contact")
    if contact is not None and contact.prospect_id != prospect.id:
        raise PermissionDenied
    try:
        _, created = create_prospect_follow_up(
            tenant=prospect.tenant,
            prospect=prospect,
            action_type=form.cleaned_data["action_type"],
            note=form.cleaned_data.get("note") or "",
            due_at=form.cleaned_data.get("due_at"),
            contact=contact,
            idempotency_key=form.cleaned_data["idempotency_key"],
            actor=request.user,
            request=request,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(f"{reverse('operations_portal:prospecting_prospect_detail', args=[prospect.id])}?new_follow_up=1")
    messages.success(request, "Próxima ação criada." if created else "Próxima ação já existia.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_complete_follow_up(request, prospect_id, follow_up_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    follow_up = get_object_or_404(_follow_up_queryset(access.tenant), pk=follow_up_id, prospect=prospect)
    try:
        complete_prospect_follow_up(tenant=prospect.tenant, follow_up=follow_up, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
    messages.success(request, "Próxima ação concluída.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_cancel_follow_up(request, prospect_id, follow_up_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    follow_up = get_object_or_404(_follow_up_queryset(access.tenant), pk=follow_up_id, prospect=prospect)
    try:
        cancel_prospect_follow_up(tenant=prospect.tenant, follow_up=follow_up, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
    messages.success(request, "Próxima ação cancelada.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_add_enrichment(request, prospect_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    form = ProspectEnrichmentCreateForm(request.POST)
    if not form.is_valid():
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
    try:
        enrichment = add_prospect_enrichment(
            tenant=prospect.tenant,
            prospect=prospect,
            field=form.cleaned_data["field"],
            value=form.cleaned_data["value"],
            source_type=form.cleaned_data["source_type"],
            source_reference=form.cleaned_data.get("source_reference") or "",
            source_url=form.cleaned_data.get("source_url") or "",
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)

    record_audit_event(
        action=ACTION_PROSPECT_ENRICHMENT_ADDED,
        actor=request.user,
        tenant=prospect.tenant,
        obj=prospect,
        metadata={
            "prospect_id": str(prospect.id),
            "enrichment_id": str(enrichment.id),
            "field": enrichment.field,
            "source_type": enrichment.source_type,
        },
        request=request,
    )
    messages.success(request, "Enriquecimento registrado.")
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_website_enrichment(request, prospect_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    prospect = get_object_or_404(_prospect_queryset(access.tenant), pk=prospect_id)
    try:
        result = enrich_prospect_from_website(tenant=prospect.tenant, prospect=prospect)
    except ValidationError as exc:
        messages.error(request, website_enrichment_error_message(exc))
        return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)

    record_audit_event(
        action=ACTION_PROSPECT_WEBSITE_ENRICHED,
        actor=request.user,
        tenant=prospect.tenant,
        obj=prospect,
        metadata={
            "prospect_id": str(prospect.id),
            "pages_fetched": result.pages_fetched,
            "emails_found": result.emails_found,
            "phones_found": result.phones_found,
            "addresses_found": result.addresses_found,
            "enrichments_created": result.enrichments_created,
            "enrichments_existing": result.enrichments_existing,
        },
        request=request,
    )
    summary = (
        f"Enriquecimento concluído. Páginas verificadas: {result.pages_fetched} "
        f"({result.pages_succeeded} com sucesso"
    )
    if result.pages_failed:
        summary += f", {result.pages_failed} com falha"
    summary += (
        f"). Emails: {result.emails_found}, telefones: {result.phones_found}, "
        f"endereços: {result.addresses_found}. "
        f"Novos registros: {result.enrichments_created}, já existentes: {result.enrichments_existing}."
    )
    messages.success(request, summary)
    if result.emails_found == 0 and result.phones_found == 0 and result.addresses_found == 0:
        messages.info(request, "Nenhum dado de contato público encontrado nesta execução.")
    for warning in result.warnings[:3]:
        messages.warning(request, warning)
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)


@login_required(login_url="/admin/login/")
@require_POST
def prospecting_bulk_website_enrichment(request):
    access = resolve_portal_access(
        request,
        capability=CAPABILITY_COMMERCIAL_MANAGE,
        allow_global=False,
        require_tenant=True,
    )
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    _ensure_posted_tenant_matches(access, request)
    raw_ids = request.POST.getlist("prospect_ids")[:BULK_WEBSITE_ENRICHMENT_LIMIT]
    if not raw_ids:
        messages.error(request, "Selecione ao menos um prospect com website.")
        return redirect("operations_portal:prospecting_prospect_list")
    prospects = list(
        _prospect_queryset(access.tenant)
        .filter(pk__in=raw_ids)
        .exclude(Q(website__isnull=True) | Q(website__exact=""))
    )
    if not prospects:
        messages.error(request, "Nenhum prospect selecionado possui website válido.")
        return redirect("operations_portal:prospecting_prospect_list")

    processed = 0
    for prospect in prospects:
        try:
            enrich_prospect_from_website(tenant=prospect.tenant, prospect=prospect)
            processed += 1
        except ValidationError:
            continue
    if processed:
        messages.success(request, f"Website enriquecido para {processed} prospect(s).")
    else:
        messages.warning(request, "Nenhum website pôde ser enriquecido (indisponível ou sem dados públicos).")
    return redirect("operations_portal:prospecting_prospect_list")
