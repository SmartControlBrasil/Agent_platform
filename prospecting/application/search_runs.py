from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import transaction

from agents.infrastructure.prospecting_configuration import ProspectingConfigurationResolver
from agents.models import AgentInstallation
from projects.models import Project
from prospecting.domain.dedupe import build_search_result_dedupe_key
from prospecting.models import SearchResult, SearchRun
from tenants.models import Tenant
from tools.application.execution import ToolExecutionError, execute_tool
from tools.models import ToolExecution

BUILD_PLAN_TOOL = "prospecting.build_search_plan"
SEARCH_TOOL = "prospecting.search_google_maps"

TERMINAL_TOOL_STATUSES = {
    ToolExecution.Status.SUCCEEDED,
    ToolExecution.Status.FAILED,
    ToolExecution.Status.CANCELLED,
    ToolExecution.Status.EXPIRED,
}


class ProspectingSearchRunError(ValidationError):
    pass


def resolve_active_prospecting_installation(*, tenant: Tenant, project: Project) -> AgentInstallation:
    queryset = AgentInstallation.objects.select_related(
        "tenant",
        "project",
        "agent_definition",
        "agent_version",
    ).filter(
        tenant=tenant,
        project=project,
        is_enabled=True,
        agent_definition__slug="prospecting",
        agent_definition__is_active=True,
        agent_version__status="active",
    )
    count = queryset.count()
    if count == 0:
        raise ProspectingSearchRunError("Nenhuma instalação ativa do Prospecting Agent foi encontrada para este projeto.")
    if count > 1:
        raise ProspectingSearchRunError(
            "Há mais de uma instalação ativa do Prospecting Agent neste projeto. Ajuste as instalações antes de iniciar a pesquisa."
        )
    return queryset.first()


def build_search_plan_for_manual_run(
    *,
    tenant: Tenant,
    project: Project,
    objective: str,
    target_region: str,
    actor=None,
    request=None,
) -> dict:
    installation = resolve_active_prospecting_installation(tenant=tenant, project=project)
    runtime_config = ProspectingConfigurationResolver().resolve(installation=installation, tenant=tenant)
    tool_result = execute_tool(
        installation=installation,
        tool_slug=BUILD_PLAN_TOOL,
        input={
            "target_market": runtime_config.target_market,
            "target_profile": runtime_config.target_profile,
            "target_region": _clean_text(target_region),
            "objective": _clean_text(objective),
        },
        actor=actor,
        request=request,
        metadata={"source": "operations_portal.prospecting.new_search"},
    )
    queries = _normalize_queries((tool_result.output or {}).get("queries"))
    return {
        "installation": installation,
        "runtime_config": runtime_config,
        "queries": queries,
    }


def create_and_dispatch_search_run(
    *,
    tenant: Tenant,
    project: Project,
    objective: str,
    target_region: str,
    selected_queries: list[str],
    actor=None,
    request=None,
) -> SearchRun:
    installation = resolve_active_prospecting_installation(tenant=tenant, project=project)
    runtime_config = ProspectingConfigurationResolver().resolve(installation=installation, tenant=tenant)
    queries = _normalize_queries(selected_queries)
    with transaction.atomic():
        search_run = SearchRun.objects.create(
            tenant=tenant,
            project=project,
            agent_installation=installation,
            target_market=runtime_config.target_market,
            target_region=_clean_text(target_region),
            target_profile=runtime_config.target_profile,
            objective=_clean_text(objective),
            queries=queries,
            max_results=runtime_config.max_results,
            locale="pt-BR",
            status=SearchRun.Status.PENDING,
        )
        tool_result = execute_tool(
            installation=installation,
            tool_slug=SEARCH_TOOL,
            input={
                "schema_version": 1,
                "queries": queries,
                "target_region": search_run.target_region,
                "max_results": search_run.max_results,
                "locale": search_run.locale,
            },
            idempotency_key=search_run.idempotency_key,
            actor=actor,
            request=request,
            metadata={
                "search_run_id": str(search_run.id),
                "objective": search_run.objective,
            },
        )
        tool_execution_id = _uuid_from_metadata(tool_result.metadata.get("tool_execution_id"))
        if not tool_execution_id:
            raise ProspectingSearchRunError("Falha ao registrar execução da pesquisa no Agent Platform.")
        search_run.agent_platform_execution_id = tool_execution_id
        if tool_result.status == "dispatched":
            search_run.transition_to(SearchRun.Status.DISPATCHED)
        search_run.save(update_fields=["agent_platform_execution_id", "status", "dispatched_at", "updated_at"])
    return synchronize_search_run(search_run=search_run)


def synchronize_search_run(*, search_run: SearchRun) -> SearchRun:
    with transaction.atomic():
        run = SearchRun.objects.select_for_update().select_related("tenant", "project", "agent_installation").get(pk=search_run.pk)
        if not run.agent_platform_execution_id:
            return run
        try:
            execution = ToolExecution.objects.select_for_update().get(
                pk=run.agent_platform_execution_id,
                tenant=run.tenant,
                project=run.project,
                agent_installation=run.agent_installation,
                tool_definition__slug=SEARCH_TOOL,
            )
        except ToolExecution.DoesNotExist as exc:
            raise ProspectingSearchRunError("Execução da pesquisa não foi encontrada para este SearchRun.") from exc

        if execution.status == ToolExecution.Status.DISPATCHED and run.status == SearchRun.Status.PENDING:
            run.transition_to(SearchRun.Status.DISPATCHED)
        elif execution.status == ToolExecution.Status.RUNNING and run.status in {SearchRun.Status.PENDING, SearchRun.Status.DISPATCHED}:
            run.transition_to(SearchRun.Status.RUNNING)
        elif execution.status == ToolExecution.Status.SUCCEEDED and run.status != SearchRun.Status.COMPLETED:
            _materialize_search_results(run=run, execution=execution)
            run.transition_to(SearchRun.Status.COMPLETED)
        elif execution.status in {ToolExecution.Status.FAILED, ToolExecution.Status.EXPIRED} and run.status != SearchRun.Status.FAILED:
            run.transition_to(
                SearchRun.Status.FAILED,
                error_code=(execution.error_code or "tool_execution_failed")[:80],
                error_message=(execution.error_message or "A execução da tool falhou.")[:500],
            )
        elif execution.status == ToolExecution.Status.CANCELLED and run.status != SearchRun.Status.CANCELLED:
            run.transition_to(SearchRun.Status.CANCELLED)
        run.save(
            update_fields=[
                "status",
                "dispatched_at",
                "completed_at",
                "failed_at",
                "error_code",
                "error_message",
                "updated_at",
            ]
        )
        return run


def _materialize_search_results(*, run: SearchRun, execution: ToolExecution) -> None:
    payload = execution.result_payload or {}
    businesses = payload.get("businesses") or []
    if not isinstance(businesses, list):
        raise ProspectingSearchRunError("Resultado da ferramenta de prospecção está inválido.")
    for item in businesses:
        if not isinstance(item, dict):
            continue
        dedupe_key = build_search_result_dedupe_key(
            external_id=item.get("external_id"),
            maps_url=item.get("maps_url"),
            name=item.get("name"),
            address=item.get("address"),
        )
        SearchResult.objects.get_or_create(
            tenant=run.tenant,
            search_run=run,
            dedupe_key=dedupe_key,
            defaults={
                "name": _clean_text(item.get("name")),
                "category": _nullable_text(item.get("category")),
                "address": _nullable_text(item.get("address")),
                "phone": _nullable_text(item.get("phone")),
                "website": _nullable_text(item.get("website")),
                "maps_url": _nullable_text(item.get("maps_url")),
                "external_id": _nullable_text(item.get("external_id")),
                "source_query": _nullable_text(item.get("source_query")),
                "raw_data": item,
            },
        )


def _normalize_queries(queries) -> list[str]:
    if not isinstance(queries, list):
        raise ProspectingSearchRunError("Plano de queries inválido.")
    normalized = []
    for query in queries:
        text = _clean_text(query)
        if text and text not in normalized:
            normalized.append(text)
    if not normalized:
        raise ProspectingSearchRunError("Selecione pelo menos uma query para executar a pesquisa.")
    if len(normalized) > 20:
        raise ProspectingSearchRunError("A pesquisa permite no máximo 20 queries por execução.")
    return normalized


def _clean_text(value) -> str:
    return " ".join(str(value or "").strip().split())


def _nullable_text(value) -> str | None:
    text = _clean_text(value)
    return text or None


def _uuid_from_metadata(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
