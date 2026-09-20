from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from agents.models import AgentInstallation
from audit.services import record_audit_event
from tools.application.registry import ToolRuntimeRegistry
from tools.domain.runtime import ToolExecutionContext, ToolRequest, ToolResult
from tools.infrastructure.registry import build_default_tool_registry
from tools.models import AgentToolBinding

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
    actor: object | None = None
    request: object | None = None


def execute_tool(
    *,
    installation: AgentInstallation,
    tool_slug: str,
    input: dict[str, Any] | None = None,
    user_id: str = "",
    metadata: dict[str, Any] | None = None,
    actor=None,
    request=None,
    registry: ToolRuntimeRegistry | None = None,
) -> ToolResult:
    registry = registry or build_default_tool_registry()
    metadata = metadata or {}
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
        runtime = registry.resolve(binding.tool_definition.runtime_handler)

    context = ToolExecutionContext(
        tenant_id=str(installation.tenant_id),
        project_id=str(installation.project_id),
        installation_id=str(installation.id),
        tool_binding_id=str(binding.id),
        user_id=str(user_id or ""),
        metadata={
            "tool_definition_id": str(binding.tool_definition_id),
            "tool_slug": binding.tool_definition.slug,
            "agent_definition": installation.agent_definition.slug,
            **metadata,
        },
    )
    request_obj = ToolRequest(
        input=dict(input or {}),
        metadata={"binding_configuration": dict(binding.configuration or {})},
    )
    result = runtime.execute(context, request_obj)
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
            "status": result.status,
        },
        request=request,
    )
    return result


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
