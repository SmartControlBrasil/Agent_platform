from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from prospecting.application.search_runs import SEARCH_TOOL, build_search_run_attempt_idempotency_key
from prospecting.models import SearchRun, SearchRunExecutionAttempt
from tools.application.execution import execute_tool
from tools.application.lifecycle import (
    ACTION_EXECUTION_DISPATCHED,
    ToolExecutionLifecycleError,
    cancel_tool_execution,
    record_tool_execution_event,
    transition_execution,
)
from tools.models import ToolExecution

TERMINAL_RETRYABLE_EXECUTION_STATUSES = {
    ToolExecution.Status.FAILED,
    ToolExecution.Status.EXPIRED,
    ToolExecution.Status.CANCELLED,
}
NON_TERMINAL_EXECUTION_STATUSES = {
    ToolExecution.Status.PENDING,
    ToolExecution.Status.DISPATCHED,
    ToolExecution.Status.RUNNING,
}
NON_RETRYABLE_EXECUTION_STATUSES = {ToolExecution.Status.SUCCEEDED}
CANCELLABLE_EXECUTION_STATUSES = {
    ToolExecution.Status.PENDING,
    ToolExecution.Status.DISPATCHED,
}


class ProspectingExecutionRecoveryError(ValidationError):
    pass


@dataclass(frozen=True)
class RetrySearchRunResult:
    search_run: SearchRun
    attempt: SearchRunExecutionAttempt
    created_new_attempt: bool


@dataclass(frozen=True)
class RedispatchSearchRunResult:
    search_run: SearchRun
    attempt: SearchRunExecutionAttempt
    transitioned_to_dispatched: bool


def redispatch_search_run(*, search_run: SearchRun, actor=None, request=None) -> RedispatchSearchRunResult:
    with transaction.atomic():
        run = SearchRun.objects.select_for_update().select_related("tenant", "project", "agent_installation").get(pk=search_run.pk)
        attempt = _current_attempt_locked(run)
        if attempt is None:
            raise ProspectingExecutionRecoveryError("SearchRun não possui tentativa para redispatch.")
        execution = ToolExecution.objects.select_for_update().get(pk=attempt.tool_execution_id)
        if execution.status in TERMINAL_RETRYABLE_EXECUTION_STATUSES | NON_RETRYABLE_EXECUTION_STATUSES:
            raise ProspectingExecutionRecoveryError("A tentativa atual é terminal; use retry para criar nova tentativa.")
        if execution.status == ToolExecution.Status.RUNNING:
            raise ProspectingExecutionRecoveryError("A tentativa atual já está em execução.")
        transitioned = False
        if execution.status == ToolExecution.Status.PENDING:
            transition_execution(execution, ToolExecution.Status.DISPATCHED)
            record_tool_execution_event(ACTION_EXECUTION_DISPATCHED, execution, actor=actor, request=request)
            transitioned = True
        if run.status != SearchRun.Status.DISPATCHED:
            run.transition_to(SearchRun.Status.DISPATCHED)
        run.agent_platform_execution_id = execution.id
        run.save(update_fields=["agent_platform_execution_id", "status", "dispatched_at", "error_code", "error_message", "updated_at"])
        return RedispatchSearchRunResult(search_run=run, attempt=attempt, transitioned_to_dispatched=transitioned)


def retry_search_run(*, search_run: SearchRun, actor=None, request=None) -> RetrySearchRunResult:
    with transaction.atomic():
        run = SearchRun.objects.select_for_update().select_related("tenant", "project", "agent_installation").get(pk=search_run.pk)
        _validate_run_installation_for_retry(run)
        current_attempt = _current_attempt_locked(run)
        if current_attempt is None:
            raise ProspectingExecutionRecoveryError("SearchRun não possui tentativa vinculada para retry.")
        current_execution = ToolExecution.objects.select_for_update().get(pk=current_attempt.tool_execution_id)
        status = current_execution.status
        if status in NON_RETRYABLE_EXECUTION_STATUSES:
            raise ProspectingExecutionRecoveryError("SearchRun concluída não permite retry.")
        if status in NON_TERMINAL_EXECUTION_STATUSES:
            return RetrySearchRunResult(search_run=run, attempt=current_attempt, created_new_attempt=False)
        if status not in TERMINAL_RETRYABLE_EXECUTION_STATUSES:
            raise ProspectingExecutionRecoveryError("Status atual não permite retry.")

        next_attempt_number = current_attempt.attempt_number + 1
        existing_next = (
            SearchRunExecutionAttempt.objects.select_for_update()
            .select_related("tool_execution")
            .filter(search_run=run, attempt_number=next_attempt_number)
            .first()
        )
        if existing_next is not None:
            run.agent_platform_execution_id = existing_next.tool_execution_id
            if run.status in {SearchRun.Status.FAILED, SearchRun.Status.CANCELLED} and existing_next.tool_execution.status != ToolExecution.Status.SUCCEEDED:
                run.transition_to(SearchRun.Status.DISPATCHED)
            run.save(update_fields=["agent_platform_execution_id", "status", "dispatched_at", "error_code", "error_message", "updated_at"])
            return RetrySearchRunResult(search_run=run, attempt=existing_next, created_new_attempt=False)

        tool_result = execute_tool(
            installation=run.agent_installation,
            tool_slug=SEARCH_TOOL,
            input={
                "schema_version": 1,
                "queries": run.queries,
                "target_region": run.target_region,
                "max_results": run.max_results,
                "locale": run.locale,
            },
            idempotency_key=build_search_run_attempt_idempotency_key(run=run, attempt_number=next_attempt_number),
            actor=actor,
            request=request,
            metadata={
                "search_run_id": str(run.id),
                "search_run_attempt": next_attempt_number,
                "retry_from_execution_id": str(current_execution.id),
            },
        )
        execution_id = (tool_result.metadata or {}).get("tool_execution_id")
        if not execution_id:
            raise ProspectingExecutionRecoveryError("Não foi possível registrar a nova tentativa da pesquisa.")
        execution = ToolExecution.objects.select_for_update().get(
            pk=execution_id,
            tenant=run.tenant,
            project=run.project,
            agent_installation=run.agent_installation,
            tool_definition__slug=SEARCH_TOOL,
        )
        try:
            attempt, created = SearchRunExecutionAttempt.objects.get_or_create(
                search_run=run,
                attempt_number=next_attempt_number,
                defaults={
                    "tenant": run.tenant,
                    "tool_execution": execution,
                },
            )
        except IntegrityError:
            attempt = SearchRunExecutionAttempt.objects.select_for_update().get(search_run=run, attempt_number=next_attempt_number)
            created = False
        if created and run.status in {SearchRun.Status.FAILED, SearchRun.Status.CANCELLED}:
            run.transition_to(SearchRun.Status.DISPATCHED)
        run.agent_platform_execution_id = attempt.tool_execution_id
        run.save(update_fields=["agent_platform_execution_id", "status", "dispatched_at", "error_code", "error_message", "updated_at"])
        return RetrySearchRunResult(search_run=run, attempt=attempt, created_new_attempt=created)


def cancel_search_run(*, search_run: SearchRun, actor=None, request=None) -> SearchRun:
    with transaction.atomic():
        run = SearchRun.objects.select_for_update().select_related("tenant", "project", "agent_installation").get(pk=search_run.pk)
        current_attempt = _current_attempt_locked(run)
        if current_attempt is None:
            raise ProspectingExecutionRecoveryError("SearchRun não possui tentativa ativa para cancelamento.")
        execution = ToolExecution.objects.select_for_update().get(pk=current_attempt.tool_execution_id)
        if execution.status == ToolExecution.Status.CANCELLED and run.status == SearchRun.Status.CANCELLED:
            return run
        if execution.status == ToolExecution.Status.RUNNING:
            raise ProspectingExecutionRecoveryError("Cancelamento de execução em RUNNING não é suportado com segurança nesta fase.")
        if execution.status in TERMINAL_RETRYABLE_EXECUTION_STATUSES | NON_RETRYABLE_EXECUTION_STATUSES:
            raise ProspectingExecutionRecoveryError("A execução atual já está finalizada e não pode ser cancelada.")
        if execution.status not in CANCELLABLE_EXECUTION_STATUSES:
            raise ProspectingExecutionRecoveryError("Status atual não permite cancelamento.")
        try:
            execution = cancel_tool_execution(execution, actor=actor, request=request)
        except ToolExecutionLifecycleError as exc:
            raise ProspectingExecutionRecoveryError("; ".join(exc.messages)) from exc
        if run.status != SearchRun.Status.CANCELLED:
            run.transition_to(SearchRun.Status.CANCELLED)
        run.agent_platform_execution_id = execution.id
        run.save(update_fields=["agent_platform_execution_id", "status", "updated_at"])
        return run


def _current_attempt_locked(run: SearchRun) -> SearchRunExecutionAttempt | None:
    attempt = (
        SearchRunExecutionAttempt.objects.select_for_update()
        .select_related("tool_execution")
        .filter(search_run=run)
        .order_by("-attempt_number")
        .first()
    )
    if attempt is not None:
        return attempt
    if not run.agent_platform_execution_id:
        return None
    execution = ToolExecution.objects.select_for_update().get(
        pk=run.agent_platform_execution_id,
        tenant=run.tenant,
        project=run.project,
        agent_installation=run.agent_installation,
        tool_definition__slug=SEARCH_TOOL,
    )
    attempt, _ = SearchRunExecutionAttempt.objects.get_or_create(
        search_run=run,
        tool_execution=execution,
        defaults={"tenant": run.tenant, "attempt_number": 1},
    )
    return attempt


def _validate_run_installation_for_retry(run: SearchRun) -> None:
    if run.agent_installation is None:
        raise ProspectingExecutionRecoveryError("SearchRun sem AgentInstallation não permite retry.")
    if not run.agent_installation.is_enabled:
        raise ProspectingExecutionRecoveryError("A AgentInstallation original está desativada; retry não pode continuar.")
    if run.agent_installation.tenant_id != run.tenant_id:
        raise ProspectingExecutionRecoveryError("A AgentInstallation original não pertence ao tenant da pesquisa.")
    if run.agent_installation.project_id != run.project_id:
        raise ProspectingExecutionRecoveryError("A AgentInstallation original não pertence ao projeto da pesquisa.")
