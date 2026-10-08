from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit_event
from tools.infrastructure.validation import (
    ToolResultValidatorRegistry,
    build_default_result_validator_registry,
    validation_error_message,
)
from tools.models import ToolExecution, ToolExecutor, ToolExecutorCapability

ACTION_EXECUTION_CREATED = "tool.execution.created"
ACTION_EXECUTION_DISPATCHED = "tool.execution.dispatched"
ACTION_EXECUTION_CLAIMED = "tool.execution.claimed"
ACTION_EXECUTION_SUCCEEDED = "tool.execution.succeeded"
ACTION_EXECUTION_FAILED = "tool.execution.failed"
ACTION_EXECUTION_PROGRESS = "tool.execution.progress"
ACTION_EXECUTION_CANCELLED = "tool.execution.cancelled"
ACTION_EXECUTION_EXPIRED = "tool.execution.expired"

TERMINAL_STATUSES = {
    ToolExecution.Status.SUCCEEDED,
    ToolExecution.Status.FAILED,
    ToolExecution.Status.CANCELLED,
    ToolExecution.Status.EXPIRED,
}

VALID_TRANSITIONS = {
    ToolExecution.Status.PENDING: {ToolExecution.Status.RUNNING, ToolExecution.Status.DISPATCHED, ToolExecution.Status.CANCELLED, ToolExecution.Status.EXPIRED},
    ToolExecution.Status.DISPATCHED: {ToolExecution.Status.RUNNING, ToolExecution.Status.CANCELLED, ToolExecution.Status.EXPIRED},
    ToolExecution.Status.RUNNING: {ToolExecution.Status.SUCCEEDED, ToolExecution.Status.FAILED, ToolExecution.Status.CANCELLED, ToolExecution.Status.EXPIRED},
    ToolExecution.Status.SUCCEEDED: set(),
    ToolExecution.Status.FAILED: set(),
    ToolExecution.Status.CANCELLED: set(),
    ToolExecution.Status.EXPIRED: set(),
}

SAFE_DIAGNOSTIC_KEYS = {
    "stage",
    "current_query",
    "query",
    "query_index",
    "pathname",
    "search",
    "feed_found",
    "has_results_feed",
    "hasResultsFeed",
    "article_count",
    "articleCount",
    "place_anchor_count",
    "placeAnchorCount",
    "zero_results",
    "has_no_results_text",
    "hasNoResultsText",
    "consent_screen",
    "has_consent_text",
    "hasConsentText",
    "captcha_or_blocked",
    "has_challenge_text",
    "hasChallengeText",
    "detected_results",
    "processed_results",
    "last_activity_at",
    "ready_state",
    "readyState",
    "has_map_main",
    "hasMapMain",
    "has_search_input",
    "hasSearchInput",
    "search_input_aria_label",
    "searchInputAriaLabel",
    "signal",
    "code",
}

PROGRESS_KEYS = {
    "stage",
    "current_query",
    "detected_results",
    "processed_results",
    "last_activity_at",
}

KEY_ALIASES = {
    "query": "current_query",
    "hasResultsFeed": "feed_found",
    "articleCount": "article_count",
    "placeAnchorCount": "place_anchor_count",
    "hasNoResultsText": "zero_results",
    "hasConsentText": "consent_screen",
    "hasChallengeText": "captcha_or_blocked",
    "readyState": "ready_state",
    "hasMapMain": "has_map_main",
    "hasSearchInput": "has_search_input",
    "searchInputAriaLabel": "search_input_aria_label",
}


class ToolExecutionLifecycleError(ValidationError):
    pass


@dataclass(frozen=True)
class ClaimResult:
    execution: ToolExecution


def transition_execution(
    execution: ToolExecution,
    status: str,
    *,
    executor: ToolExecutor | None = None,
    result_payload: dict[str, Any] | None = None,
    error_code: str = "",
    error_message: str = "",
    save: bool = True,
) -> ToolExecution:
    if status not in VALID_TRANSITIONS.get(execution.status, set()):
        raise ToolExecutionLifecycleError(f"Invalid tool execution transition: {execution.status} -> {status}.")
    now = timezone.now()
    execution.status = status
    if executor is not None:
        execution.executor = executor
    if status == ToolExecution.Status.RUNNING and execution.started_at is None:
        execution.started_at = now
    if status in TERMINAL_STATUSES:
        execution.completed_at = now
    if result_payload is not None:
        execution.result_payload = result_payload
    if error_code:
        execution.error_code = sanitize_error_text(error_code, 80)
    if error_message:
        execution.error_message = sanitize_error_text(error_message, 500)
    if save:
        execution.save()
    return execution


def create_tool_execution(
    *,
    binding,
    request_payload: dict[str, Any],
    idempotency_key: str = "",
    expires_at=None,
    actor=None,
    request=None,
    requested_by_service_client=None,
) -> tuple[ToolExecution, bool]:
    idempotency_key = normalize_idempotency_key(idempotency_key)
    defaults = {
        "tenant": binding.tenant,
        "project": binding.project,
        "agent_installation": binding.agent_installation,
        "tool_binding": binding,
        "tool_definition": binding.tool_definition,
        "execution_mode": binding.tool_definition.execution_mode,
        "status": ToolExecution.Status.PENDING,
        "request_payload": request_payload,
        "expires_at": expires_at,
        "requested_by_service_client": requested_by_service_client,
    }
    if idempotency_key:
        execution, created = ToolExecution.objects.get_or_create(
            tenant=binding.tenant,
            agent_installation=binding.agent_installation,
            tool_definition=binding.tool_definition,
            idempotency_key=idempotency_key,
            defaults=defaults,
        )
    else:
        execution = ToolExecution.objects.create(idempotency_key="", **defaults)
        created = True
    if created:
        record_tool_execution_event(ACTION_EXECUTION_CREATED, execution, actor=actor, request=request)
    return execution, created


def claim_tool_execution(*, executor: ToolExecutor, execution: ToolExecution, actor=None, request=None) -> ToolExecution:
    with transaction.atomic():
        execution = ToolExecution.objects.select_for_update().get(pk=execution.pk)
        executor = ToolExecutor.objects.select_for_update().get(pk=executor.pk)
        if execution.expires_at is not None and execution.expires_at <= timezone.now():
            transition_execution(execution, ToolExecution.Status.EXPIRED)
            expired_execution = execution
        else:
            expired_execution = None
            _validate_claim(executor=executor, execution=execution)
            execution = transition_execution(execution, ToolExecution.Status.RUNNING, executor=executor)
    if expired_execution is not None:
        raise ToolExecutionLifecycleError("Tool execution is expired.")
    record_tool_execution_event(ACTION_EXECUTION_CLAIMED, execution, actor=actor, request=request)
    return execution


def complete_tool_execution(
    *,
    executor: ToolExecutor,
    execution: ToolExecution,
    result: dict[str, Any] | None = None,
    actor=None,
    request=None,
    result_validators: ToolResultValidatorRegistry | None = None,
) -> ToolExecution:
    result_validators = result_validators or build_default_result_validator_registry()
    with transaction.atomic():
        execution = ToolExecution.objects.select_for_update().get(pk=execution.pk)
        executor = ToolExecutor.objects.get(pk=executor.pk)
        _validate_executor_owns_running_execution(executor=executor, execution=execution)
        try:
            result_payload = result_validators.validate(
                tool_slug=execution.tool_definition.slug,
                result=dict(result or {}),
                execution=execution,
            )
        except ValidationError as exc:
            execution = transition_execution(
                execution,
                ToolExecution.Status.FAILED,
                error_code="invalid_tool_result",
                error_message=validation_error_message(exc),
            )
            record_tool_execution_event(ACTION_EXECUTION_FAILED, execution, actor=actor, request=request)
            return execution
        execution = transition_execution(
            execution,
            ToolExecution.Status.SUCCEEDED,
            result_payload=result_payload,
        )
    record_tool_execution_event(ACTION_EXECUTION_SUCCEEDED, execution, actor=actor, request=request)
    return execution


def fail_tool_execution(
    *,
    executor: ToolExecutor,
    execution: ToolExecution,
    error_code: str,
    error_message: str = "",
    diagnostics: dict[str, Any] | None = None,
    actor=None,
    request=None,
) -> ToolExecution:
    with transaction.atomic():
        execution = ToolExecution.objects.select_for_update().get(pk=execution.pk)
        executor = ToolExecutor.objects.get(pk=executor.pk)
        _validate_executor_owns_running_execution(executor=executor, execution=execution)
        result_payload = _merge_execution_metadata(execution.result_payload, diagnostics=diagnostics)
        execution = transition_execution(
            execution,
            ToolExecution.Status.FAILED,
            result_payload=result_payload,
            error_code=error_code,
            error_message=error_message,
        )
    record_tool_execution_event(ACTION_EXECUTION_FAILED, execution, actor=actor, request=request)
    return execution


def record_tool_execution_progress(
    *,
    executor: ToolExecutor,
    execution: ToolExecution,
    progress: dict[str, Any] | None = None,
    actor=None,
    request=None,
) -> ToolExecution:
    with transaction.atomic():
        execution = ToolExecution.objects.select_for_update().get(pk=execution.pk)
        executor = ToolExecutor.objects.get(pk=executor.pk)
        _validate_executor_owns_running_execution(executor=executor, execution=execution)
        execution.result_payload = _merge_execution_metadata(execution.result_payload, progress=progress)
        execution.save(update_fields=["result_payload", "updated_at"])
    record_tool_execution_event(ACTION_EXECUTION_PROGRESS, execution, actor=actor, request=request)
    return execution


def _merge_execution_metadata(
    result_payload: dict[str, Any] | None,
    *,
    diagnostics: dict[str, Any] | None = None,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(result_payload or {}) if isinstance(result_payload, dict) else {}
    sanitized_progress = sanitize_execution_progress(progress)
    if sanitized_progress:
        payload["progress"] = sanitized_progress
    sanitized_diagnostics = sanitize_execution_diagnostics(diagnostics)
    if sanitized_diagnostics:
        payload["diagnostics"] = sanitized_diagnostics
        if "progress" not in payload:
            derived_progress = sanitize_execution_progress(sanitized_diagnostics)
            if derived_progress:
                payload["progress"] = derived_progress
    return payload


def sanitize_execution_progress(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    sanitized = {}
    for key in PROGRESS_KEYS:
        normalized = _normalize_diagnostic_value(key, value.get(key))
        if normalized is not None:
            sanitized[key] = normalized
    if "last_activity_at" not in sanitized:
        sanitized["last_activity_at"] = timezone.now().isoformat()
    return sanitized


def sanitize_execution_diagnostics(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    sanitized = {}
    for key, raw_value in value.items():
        if key == "diagnostics" and isinstance(raw_value, dict):
            nested = sanitize_execution_diagnostics(raw_value)
            sanitized.update(nested)
            continue
        if key not in SAFE_DIAGNOSTIC_KEYS:
            continue
        normalized_key = KEY_ALIASES.get(key, key)
        normalized = _normalize_diagnostic_value(normalized_key, raw_value)
        if normalized is not None:
            sanitized[normalized_key] = normalized
    if "last_activity_at" not in sanitized:
        sanitized["last_activity_at"] = timezone.now().isoformat()
    return sanitized


def _normalize_diagnostic_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if key in {"detected_results", "processed_results", "article_count", "place_anchor_count", "query_index"}:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None
    if isinstance(value, (int, float)):
        return value
    text = " ".join(str(value or "").split())
    if not text:
        return None
    limits = {
        "current_query": 180,
        "pathname": 160,
        "search": 160,
        "search_input_aria_label": 120,
        "last_activity_at": 80,
        "stage": 80,
        "signal": 80,
        "code": 80,
        "ready_state": 40,
    }
    return sanitize_error_text(text, limits.get(key, 120))


def expire_tool_execution(execution: ToolExecution, *, actor=None, request=None) -> ToolExecution:
    if execution.status == ToolExecution.Status.EXPIRED:
        return execution
    execution = transition_execution(execution, ToolExecution.Status.EXPIRED)
    record_tool_execution_event(ACTION_EXECUTION_EXPIRED, execution, actor=actor, request=request)
    return execution


def cancel_tool_execution(execution: ToolExecution, *, actor=None, request=None) -> ToolExecution:
    if execution.status == ToolExecution.Status.CANCELLED:
        return execution
    if execution.status == ToolExecution.Status.RUNNING:
        raise ToolExecutionLifecycleError("Running tool executions cannot be cancelled safely.")
    execution = transition_execution(execution, ToolExecution.Status.CANCELLED)
    record_tool_execution_event(ACTION_EXECUTION_CANCELLED, execution, actor=actor, request=request)
    return execution


def record_tool_execution_event(action: str, execution: ToolExecution, *, actor=None, request=None) -> None:
    record_audit_event(
        action=action,
        actor=actor,
        tenant=execution.tenant,
        obj=execution,
        metadata={
            "tool_execution_id": str(execution.pk),
            "tenant_id": execution.tenant_id,
            "project_id": str(execution.project_id),
            "installation_id": str(execution.agent_installation_id),
            "tool_binding_id": str(execution.tool_binding_id),
            "tool_definition_id": str(execution.tool_definition_id),
            "executor_id": str(execution.executor_id or ""),
            "requested_by_service_client_id": str(execution.requested_by_service_client_id or ""),
            "execution_mode": execution.execution_mode,
            "status": execution.status,
        },
        request=request,
    )


def normalize_idempotency_key(value: str | None) -> str:
    return " ".join(str(value or "").strip().split())[:160]


def sanitize_error_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    blocked = ("password", "token", "secret", "api_key", "apikey", "authorization", "cookie", "credential")
    lowered = text.lower()
    if any(fragment in lowered for fragment in blocked):
        return "[sanitized]"
    return text[:limit]


def _validate_claim(*, executor: ToolExecutor, execution: ToolExecution) -> None:
    if not executor.is_active:
        raise ToolExecutionLifecycleError("Executor must be active to claim tool executions.")
    if execution.execution_mode != "DELEGATED":
        raise ToolExecutionLifecycleError("Only delegated tool executions can be claimed.")
    if execution.status != ToolExecution.Status.DISPATCHED:
        raise ToolExecutionLifecycleError("Only dispatched tool executions can be claimed.")
    if executor.tenant_id is not None and executor.tenant_id != execution.tenant_id:
        raise ToolExecutionLifecycleError("Executor tenant does not match tool execution tenant.")
    if not ToolExecutorCapability.objects.filter(
        executor=executor,
        tool_definition=execution.tool_definition,
        is_enabled=True,
    ).exists():
        raise ToolExecutionLifecycleError("Executor is not authorized for this tool.")
    if execution.executor_id is not None:
        raise ToolExecutionLifecycleError("Tool execution is already claimed.")


def _validate_executor_owns_running_execution(*, executor: ToolExecutor, execution: ToolExecution) -> None:
    if not executor.is_active:
        raise ToolExecutionLifecycleError("Executor must be active to complete tool executions.")
    if execution.status != ToolExecution.Status.RUNNING:
        raise ToolExecutionLifecycleError("Only running tool executions can be completed or failed.")
    if execution.executor_id != executor.pk:
        raise ToolExecutionLifecycleError("Executor does not own this tool execution.")
