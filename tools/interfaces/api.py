from __future__ import annotations

import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from agents.models import AgentInstallation
from tenants.access import CAPABILITY_TENANT_VIEW, get_accessible_tenants, require_tenant_capability
from tools.application.execution import execute_tool
from tools.application.lifecycle import (
    ToolExecutionLifecycleError,
    claim_tool_execution,
    complete_tool_execution,
    fail_tool_execution,
)
from tools.application.registry import UnknownToolRuntime
from tools.models import AgentToolBinding, ToolDefinition, ToolExecution, ToolExecutor


def _json_auth_required(request):
    if not getattr(request.user, "is_authenticated", False):
        return JsonResponse({"error": "authentication_required"}, status=401)
    return None


def _accessible_tenant_ids(user):
    return list(get_accessible_tenants(user).values_list("id", flat=True))


def _tool_payload(tool):
    return {
        "id": str(tool.id),
        "slug": tool.slug,
        "name": tool.name,
        "description": tool.description,
        "category": tool.category,
        "execution_mode": tool.execution_mode,
        "is_active": tool.is_active,
    }


def _execution_payload(execution):
    duration = None
    if execution.started_at and execution.completed_at:
        duration = (execution.completed_at - execution.started_at).total_seconds()
    return {
        "id": str(execution.id),
        "tenant_id": execution.tenant_id,
        "project_id": str(execution.project_id),
        "agent_installation_id": str(execution.agent_installation_id),
        "tool_binding_id": str(execution.tool_binding_id),
        "tool_definition": _tool_payload(execution.tool_definition),
        "execution_mode": execution.execution_mode,
        "status": execution.status,
        "executor_id": str(execution.executor_id or ""),
        "error_code": execution.error_code,
        "error_message": execution.error_message,
        "created_at": execution.created_at.isoformat(),
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
        "expires_at": execution.expires_at.isoformat() if execution.expires_at else None,
        "duration_seconds": duration,
    }


def _get_executor_from_payload(payload, user):
    public_id = str(payload.get("executor_public_id") or "").strip()
    if not public_id:
        raise ToolExecutionLifecycleError("executor_public_id is required.")
    executor = ToolExecutor.objects.select_related("tenant").get(public_id=public_id)
    if executor.tenant_id is not None and executor.tenant_id not in _accessible_tenant_ids(user):
        raise ToolExecutor.DoesNotExist
    return executor


def _binding_payload(binding):
    return {
        "id": str(binding.id),
        "tenant_id": binding.tenant_id,
        "project_id": str(binding.project_id),
        "agent_installation_id": str(binding.agent_installation_id),
        "tool_definition": _tool_payload(binding.tool_definition),
        "is_enabled": binding.is_enabled,
        "configuration": binding.configuration,
    }


@require_http_methods(["GET"])
def tools_list(request):
    auth = _json_auth_required(request)
    if auth:
        return auth
    return JsonResponse({"results": [_tool_payload(tool) for tool in ToolDefinition.objects.filter(is_active=True)]})


@require_http_methods(["GET"])
def installation_tools(request, installation_id):
    auth = _json_auth_required(request)
    if auth:
        return auth
    try:
        installation = AgentInstallation.objects.get(pk=installation_id, tenant_id__in=_accessible_tenant_ids(request.user))
        require_tenant_capability(request.user, installation.tenant, CAPABILITY_TENANT_VIEW)
    except AgentInstallation.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except PermissionDenied:
        return JsonResponse({"error": "forbidden"}, status=403)
    bindings = AgentToolBinding.objects.filter(agent_installation=installation).select_related("tool_definition")
    return JsonResponse({"results": [_binding_payload(binding) for binding in bindings]})


@require_http_methods(["POST"])
def execute_binding(request, binding_id):
    auth = _json_auth_required(request)
    if auth:
        return auth
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)
    try:
        binding = AgentToolBinding.objects.select_related(
            "tenant", "project", "agent_installation", "tool_definition"
        ).get(pk=binding_id, tenant_id__in=_accessible_tenant_ids(request.user))
        require_tenant_capability(request.user, binding.tenant, CAPABILITY_TENANT_VIEW)
        result = execute_tool(
            installation=binding.agent_installation,
            tool_slug=binding.tool_definition.slug,
            input=payload.get("input") or {},
            user_id=str(request.user.pk),
            actor=request.user,
            request=request,
        )
    except AgentToolBinding.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except PermissionDenied:
        return JsonResponse({"error": "forbidden"}, status=403)
    except UnknownToolRuntime:
        return JsonResponse({"error": "unknown_tool_runtime"}, status=500)
    except ValidationError as exc:
        return JsonResponse({"error": "tool_not_executable", "details": exc.messages}, status=400)
    return JsonResponse({"status": result.status, "output": result.output, "metadata": result.metadata})


@require_http_methods(["GET"])
def execution_list(request):
    auth = _json_auth_required(request)
    if auth:
        return auth
    executions = ToolExecution.objects.filter(tenant_id__in=_accessible_tenant_ids(request.user)).select_related(
        "tool_definition", "agent_installation", "project", "executor"
    )
    return JsonResponse({"results": [_execution_payload(execution) for execution in executions[:100]]})


@require_http_methods(["GET"])
def execution_detail(request, execution_id):
    auth = _json_auth_required(request)
    if auth:
        return auth
    try:
        execution = ToolExecution.objects.select_related(
            "tool_definition", "agent_installation", "project", "executor"
        ).get(pk=execution_id, tenant_id__in=_accessible_tenant_ids(request.user))
        require_tenant_capability(request.user, execution.tenant, CAPABILITY_TENANT_VIEW)
    except ToolExecution.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except PermissionDenied:
        return JsonResponse({"error": "forbidden"}, status=403)
    return JsonResponse(_execution_payload(execution))


@require_http_methods(["POST"])
def claim_execution(request, execution_id):
    auth = _json_auth_required(request)
    if auth:
        return auth
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)
    try:
        execution = ToolExecution.objects.get(pk=execution_id, tenant_id__in=_accessible_tenant_ids(request.user))
        require_tenant_capability(request.user, execution.tenant, CAPABILITY_TENANT_VIEW)
        executor = _get_executor_from_payload(payload, request.user)
        execution = claim_tool_execution(executor=executor, execution=execution, actor=request.user, request=request)
    except (ToolExecution.DoesNotExist, ToolExecutor.DoesNotExist):
        return JsonResponse({"error": "not_found"}, status=404)
    except PermissionDenied:
        return JsonResponse({"error": "forbidden"}, status=403)
    except ValidationError as exc:
        return JsonResponse({"error": "claim_rejected", "details": exc.messages}, status=400)
    return JsonResponse(_execution_payload(execution))


@require_http_methods(["POST"])
def complete_execution(request, execution_id):
    auth = _json_auth_required(request)
    if auth:
        return auth
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)
    try:
        execution = ToolExecution.objects.get(pk=execution_id, tenant_id__in=_accessible_tenant_ids(request.user))
        require_tenant_capability(request.user, execution.tenant, CAPABILITY_TENANT_VIEW)
        executor = _get_executor_from_payload(payload, request.user)
        execution = complete_tool_execution(
            executor=executor,
            execution=execution,
            result=payload.get("result") or {},
            actor=request.user,
            request=request,
        )
    except (ToolExecution.DoesNotExist, ToolExecutor.DoesNotExist):
        return JsonResponse({"error": "not_found"}, status=404)
    except PermissionDenied:
        return JsonResponse({"error": "forbidden"}, status=403)
    except ValidationError as exc:
        return JsonResponse({"error": "completion_rejected", "details": exc.messages}, status=400)
    return JsonResponse(_execution_payload(execution))


@require_http_methods(["POST"])
def fail_execution(request, execution_id):
    auth = _json_auth_required(request)
    if auth:
        return auth
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)
    try:
        execution = ToolExecution.objects.get(pk=execution_id, tenant_id__in=_accessible_tenant_ids(request.user))
        require_tenant_capability(request.user, execution.tenant, CAPABILITY_TENANT_VIEW)
        executor = _get_executor_from_payload(payload, request.user)
        execution = fail_tool_execution(
            executor=executor,
            execution=execution,
            error_code=payload.get("error_code") or "executor_failed",
            error_message=payload.get("error_message") or "",
            actor=request.user,
            request=request,
        )
    except (ToolExecution.DoesNotExist, ToolExecutor.DoesNotExist):
        return JsonResponse({"error": "not_found"}, status=404)
    except PermissionDenied:
        return JsonResponse({"error": "forbidden"}, status=403)
    except ValidationError as exc:
        return JsonResponse({"error": "failure_rejected", "details": exc.messages}, status=400)
    return JsonResponse(_execution_payload(execution))
