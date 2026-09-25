from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from audit.services import record_audit_event
from operations_portal.access import portal_template_context, require_portal_capability, resolve_portal_access
from operations_portal.prospecting_forms import ProspectEnrichmentCreateForm, ProspectFilterForm, SearchRunFilterForm
from operations_portal.selectors import clean_querystring
from projects.models import Project
from prospecting.application.enrichments import add_prospect_enrichment
from prospecting.application.prospects import promote_search_result_to_prospect
from prospecting.application.website_enrichment import enrich_prospect_from_website
from prospecting.models import Prospect, ProspectEnrichment, ProspectSource, SearchResult, SearchRun
from tenants.access import CAPABILITY_COMMERCIAL_MANAGE, CAPABILITY_COMMERCIAL_VIEW

ACTION_PROSPECT_PROMOTED = "prospecting.search_result.promoted"
ACTION_PROSPECT_ENRICHMENT_ADDED = "prospecting.enrichment.added"
ACTION_PROSPECT_WEBSITE_ENRICHED = "prospecting.enrichment.website_requested"


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
    context = {
        "active_section": "prospeccao",
        "form": form,
        "page_obj": page_obj,
        "querystring": clean_querystring(request.GET),
        "can_manage": access.is_global or CAPABILITY_COMMERCIAL_MANAGE in access.capabilities,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/search_run_list.html", context)


@login_required(login_url="/admin/login/")
def prospecting_search_run_detail(request, run_id):
    access = resolve_portal_access(request, capability=CAPABILITY_COMMERCIAL_VIEW, allow_global=True)
    search_run = get_object_or_404(_search_run_queryset(access.tenant), pk=run_id)
    result_ids = list(search_run.results.values_list("id", flat=True))
    source_map = {
        source.search_result_id: source
        for source in ProspectSource.objects.filter(search_result_id__in=result_ids).select_related("prospect", "search_run")
    }
    results = list(search_run.results.order_by("name"))
    for result in results:
        result.promoted_source = source_map.get(result.id)

    context = {
        "active_section": "prospeccao",
        "search_run": search_run,
        "results": results,
        "can_manage": access.is_global or CAPABILITY_COMMERCIAL_MANAGE in access.capabilities,
    }
    context.update(portal_template_context(access))
    return render(request, "operations_portal/prospecting/search_run_detail.html", context)


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
