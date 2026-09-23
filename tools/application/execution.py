from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from agents.models import AgentInstallation
from audit.services import record_audit_event
from tools.application.dispatch import DelegatedToolDispatcherPort, NoopDelegatedToolDispatcher
from tools.application.lifecycle import (
    ACTION_EXECUTION_FAILED,
    ACTION_EXECUTION_SUCCEEDED,
    create_tool_execution,
    record_tool_execution_event,
    transition_execution,
)
from tools.application.registry import ToolRuntimeRegistry
from tools.domain.runtime import ToolExecutionContext, ToolRequest, ToolResult
from tools.infrastructure.registry import build_default_tool_registry
from tools.infrastructure.validation import ToolInputValidatorRegistry, build_default_input_validator_registry
from tools.models import AgentToolBinding, ToolDefinition, ToolExecution

ACTION_TOOL_EXECUTED = "tool.executed"


class ToolExecutionError(ValidationError):
    pass


@dataclass(frozen=True)
class ExecuteToolCommand:
    installation: AgentInstallation
    tool_slug: str
    input: dict[str, Any]
    user_id: str = ""
    metadata: dict[str, Any] | None = None
    idempotency_key: str = ""
    actor: object | None = None
    request: object | None = None


def execute_tool(
    *,
    installation: AgentInstallation,
    tool_slug: str,
    input: dict[str, Any] | None = None,
    user_id: str = "",
    metadata: dict[str, Any] | None = None,
    idempotency_key: str = "",
    expires_at=None,
    actor=None,
    request=None,
    requested_by_service_client=None,
    registry: ToolRuntimeRegistry | None = None,
    dispatcher: DelegatedToolDispatcherPort | None = None,
    input_validators: ToolInputValidatorRegistry | None = None,
) -> ToolResult:
    registry = registry or build_default_tool_registry()
    dispatcher = dispatcher or NoopDelegatedToolDispatcher()
    input_validators = input_validators or build_default_input_validator_registry()
    metadata = metadata or {}
    input_payload = input_validators.validate(tool_slug=tool_slug, payload=dict(input or {}))
    with transaction.atomic():
        installation = AgentInstallation.objects.select_related(
            "tenant", "project", "agent_definition", "agent_version"
        ).select_for_update().get(pk=installation.pk)
        _validate_installation_can_execute_tools(installation)
        try:
            binding = AgentToolBinding.objects.select_related(
                "tenant", "project", "agent_installation", "tool_definition"
            ).select_for_update().get(
                agent_installation=installation,
                tenant=installation.tenant,
                project=installation.project,
                tool_definition__slug=tool_slug,
            )
        except AgentToolBinding.DoesNotExist as exc:
            raise ToolExecutionError("Tool is not authorized for this agent installation.") from exc
        _validate_binding_can_execute(binding)
        execution, created = create_tool_execution(
            binding=binding,
            request_payload={"input": input_payload, "metadata": metadata},
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            actor=actor,
            request=request,
            requested_by_service_client=requested_by_service_client,
        )
        if not created:
            return _result_from_existing_execution(execution)

    if binding.tool_definition.execution_mode == ToolDefinition.ExecutionMode.DELEGATED:
        execution = dispatcher.dispatch(execution, actor=actor, request=request)
        return _delegated_result(execution)

    return _execute_local_tool(
        installation=installation,
        binding=binding,
        execution=execution,
        input_payload=input_payload,
        metadata=metadata,
        user_id=user_id,
        actor=actor,
        request=request,
        registry=registry,
    )


def _execute_local_tool(
    *,
    installation: AgentInstallation,
    binding: AgentToolBinding,
    execution: ToolExecution,
    input_payload: dict[str, Any],
    metadata: dict[str, Any],
    user_id: str,
    actor,
    request,
    registry: ToolRuntimeRegistry,
) -> ToolResult:
    try:
        transition_execution(execution, ToolExecution.Status.RUNNING)
        runtime = registry.resolve(binding.tool_definition.runtime_handler)
        context = ToolExecutionContext(
            tenant_id=str(installation.tenant_id),
            project_id=str(installation.project_id),
            installation_id=str(installation.id),
            tool_binding_id=str(binding.id),
            user_id=str(user_id or ""),
            metadata={
                "tool_definition_id": str(binding.tool_definition_id),
                "tool_execution_id": str(execution.pk),
                "tool_slug": binding.tool_definition.slug,
                "agent_definition": installation.agent_definition.slug,
                **metadata,
            },
        )
        request_obj = ToolRequest(
            input=input_payload,
            metadata={"binding_configuration": dict(binding.configuration or {})},
        )
        result = runtime.execute(context, request_obj)
    except Exception as exc:
        transition_execution(
            execution,
            ToolExecution.Status.FAILED,
            error_code=exc.__class__.__name__,
            error_message=str(exc),
        )
        record_tool_execution_event(ACTION_EXECUTION_FAILED, execution, actor=actor, request=request)
        raise

    result_metadata = {**result.metadata, "tool_execution_id": str(execution.pk)}
    transition_execution(
        execution,
        ToolExecution.Status.SUCCEEDED,
        result_payload={"status": result.status, "output": result.output, "metadata": result_metadata},
    )
    record_tool_execution_event(ACTION_EXECUTION_SUCCEEDED, execution, actor=actor, request=request)
    record_audit_event(
        action=ACTION_TOOL_EXECUTED,
        actor=actor,
        tenant=installation.tenant,
        obj=binding,
        metadata={
            "tenant_id": installation.tenant_id,
            "project_id": str(installation.project_id),
            "installation_id": str(installation.id),
            "tool_definition_id": str(binding.tool_definition_id),
            "binding_id": str(binding.id),
            "tool_execution_id": str(execution.pk),
            "status": result.status,
        },
        request=request,
    )
    return ToolResult(status=result.status, output=result.output, metadata=result_metadata)


def _result_from_existing_execution(execution: ToolExecution) -> ToolResult:
    if execution.status == ToolExecution.Status.SUCCEEDED and isinstance(execution.result_payload, dict):
        return ToolResult(
            status=execution.result_payload.get("status") or "succeeded",
            output=execution.result_payload.get("output") or {},
            metadata=execution.result_payload.get("metadata") or {"tool_execution_id": str(execution.pk)},
        )
    if execution.execution_mode == ToolDefinition.ExecutionMode.DELEGATED:
        return _delegated_result(execution)
    return ToolResult(
        status=execution.status.lower(),
        output={"status": execution.status.lower(), "tool_execution_id": str(execution.pk)},
        metadata={"tool_execution_id": str(execution.pk), "execution_status": execution.status},
    )


def _delegated_result(execution: ToolExecution) -> ToolResult:
    return ToolResult(
        status="dispatched" if execution.status == ToolExecution.Status.DISPATCHED else execution.status.lower(),
        output={
            "status": execution.status.lower(),
            "tool_execution_id": str(execution.pk),
            "execution_mode": execution.execution_mode,
        },
        metadata={
            "tool_execution_id": str(execution.pk),
            "execution_status": execution.status,
            "execution_mode": execution.execution_mode,
        },
    )


def _validate_installation_can_execute_tools(installation: AgentInstallation) -> None:
    if not installation.tenant.is_active:
        raise ToolExecutionError("Tenant must be active to execute tools.")
    if not installation.project.is_active:
        raise ToolExecutionError("Project must be active to execute tools.")
    if not installation.is_enabled:
        raise ToolExecutionError("Agent installation must be enabled to execute tools.")


def _validate_binding_can_execute(binding: AgentToolBinding) -> None:
    if binding.tenant_id != binding.agent_installation.tenant_id:
        raise ToolExecutionError("Tool binding tenant does not match installation tenant.")
    if binding.project_id != binding.agent_installation.project_id:
        raise ToolExecutionError("Tool binding project does not match installation project.")
    if not binding.is_enabled:
        raise ToolExecutionError("Tool binding is disabled.")
    if not binding.tool_definition.is_active:
        raise ToolExecutionError("Tool definition is inactive.")
