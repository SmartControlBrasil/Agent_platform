from __future__ import annotations

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.services import record_audit_event
from operations_portal.access import portal_template_context, require_portal_capability, resolve_portal_access
from operations_portal.prospecting_forms import (
    ProspectEnrichmentCreateForm,
    ProspectFilterForm,
    ProspectingSearchPlanForm,
    ProspectingSearchQueryReviewForm,
    SearchRunFilterForm,
)
from operations_portal.selectors import clean_querystring
from projects.models import Project
from prospecting.application.enrichments import add_prospect_enrichment
from prospecting.application.prospects import promote_search_result_to_prospect
from prospecting.application.search_runs import (
    ProspectingSearchRunError,
    build_search_plan_for_manual_run,
    create_and_dispatch_search_run,
    synchronize_search_run,
)
from prospecting.application.website_enrichment import enrich_prospect_from_website
from prospecting.models import Prospect, ProspectEnrichment, ProspectSource, SearchResult, SearchRun
from tenants.access import CAPABILITY_COMMERCIAL_MANAGE, CAPABILITY_COMMERCIAL_VIEW
from tools.infrastructure.validation import summarize_tool_execution
from tools.models import ToolExecution

ACTION_PROSPECT_PROMOTED = "prospecting.search_result.promoted"
ACTION_PROSPECT_ENRICHMENT_ADDED = "prospecting.enrichment.added"
ACTION_PROSPECT_WEBSITE_ENRICHED = "prospecting.enrichment.website_requested"
SESSION_SEARCH_DRAFTS_KEY = "operations_portal_prospecting_drafts"
SESSION_SEARCH_DRAFT_EXECUTIONS_KEY = "operations_portal_prospecting_draft_executions"
SEARCH_RUN_STATUS_UI = {
    SearchRun.Status.PENDING: {"label": "Preparando", "tone": "secondary"},
    SearchRun.Status.DISPATCHED: {"label": "Enviada", "tone": "info"},
    SearchRun.Status.RUNNING: {"label": "Executando", "tone": "primary"},
    SearchRun.Status.COMPLETED: {"label": "Concluída", "tone": "success"},
    SearchRun.Status.FAILED: {"label": "Falhou", "tone": "danger"},
    SearchRun.Status.CANCELLED: {"label": "Cancelada", "tone": "warning"},
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


def _prospect_queryset(tenant):
    queryset = Prospect.objects.select_related("tenant").prefetch_related("sources", "enrichments")
    if tenant is not None:
        queryset = queryset.filter(tenant=tenant)
    return queryset


def _search_run_ui(status):
    return SEARCH_RUN_STATUS_UI.get(status, {"label": status, "tone": "secondary"})


def _enrich_run_ui(search_run):
    ui = _search_run_ui(search_run.status)
    search_run.status_label = ui["label"]
    search_run.status_tone = ui["tone"]
    return search_run


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
    for run in page_obj.object_list:
        _enrich_run_ui(run)
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
    _mark_draft_executed(request, draft_id=draft_id, search_run_id=search_run.id)
    messages.success(request, "Pesquisa criada e enviada para execução.")
    return redirect("operations_portal:prospecting_search_run_detail", run_id=search_run.id)


@login_required(login_url="/admin/login/")
def prospecting_search_run_detail(request, run_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    search_run = get_object_or_404(_search_run_queryset(access.tenant), pk=run_id)
    _enrich_run_ui(search_run)
    result_ids = list(search_run.results.values_list("id", flat=True))
    source_map = {
        source.search_result_id: source
        for source in ProspectSource.objects.filter(search_result_id__in=result_ids).select_related("prospect", "search_run")
    }
    results = list(search_run.results.order_by("name"))
    for result in results:
        result.promoted_source = source_map.get(result.id)
    execution = None
    execution_summary = None
    if search_run.agent_platform_execution_id:
        execution = ToolExecution.objects.filter(
            pk=search_run.agent_platform_execution_id,
            tenant=search_run.tenant,
            project=search_run.project,
            agent_installation=search_run.agent_installation,
        ).select_related("executor", "tool_definition").first()
        if execution is not None:
            execution_summary = summarize_tool_execution(execution)

    context = {
        "active_section": "prospeccao",
        "search_run": search_run,
        "results": results,
        "execution": execution,
        "execution_summary": execution_summary,
        "can_manage": access.is_global or CAPABILITY_COMMERCIAL_MANAGE in access.capabilities,
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
def prospecting_promote_search_result(request, result_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_MANAGE, allow_global=True)
    require_portal_capability(access, CAPABILITY_COMMERCIAL_MANAGE)
    search_result = get_object_or_404(_search_result_queryset(access.tenant), pk=result_id)
    try:
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
def prospecting_prospect_list(request):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    queryset = _prospect_queryset(access.tenant)
    form = ProspectFilterForm(request.GET or None, project_queryset=_project_queryset_for_tenant(access.tenant))
    if form.is_valid():
        project = form.cleaned_data.get("project")
        query = (form.cleaned_data.get("q") or "").strip()
        if project:
            queryset = queryset.filter(sources__search_run__project=project)
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
    context = {
        "active_section": "prospeccao",
        "prospect": prospect,
        "sources": list(prospect.sources.all().order_by("-created_at")),
        "enrichments": list(prospect.enrichments.all().order_by("-observed_at", "-created_at")),
        "manual_form": ProspectEnrichmentCreateForm(
            initial={
                "field": ProspectEnrichment.Field.EMAIL,
                "source_type": ProspectEnrichment.SourceType.MANUAL,
            }
        ),
        "can_manage": access.is_global or CAPABILITY_COMMERCIAL_MANAGE in access.capabilities,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/prospect_detail.html", context)


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
        messages.error(request, "; ".join(exc.messages))
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
            "enrichments_created": result.enrichments_created,
            "enrichments_existing": result.enrichments_existing,
        },
        request=request,
    )
    messages.success(
        request,
        (
            f"Website enrichment concluído. Páginas: {result.pages_fetched}, "
            f"novos dados: {result.enrichments_created}, existentes: {result.enrichments_existing}."
        ),
    )
    for warning in result.warnings[:3]:
        messages.warning(request, warning)
    return redirect("operations_portal:prospecting_prospect_detail", prospect_id=prospect.id)
