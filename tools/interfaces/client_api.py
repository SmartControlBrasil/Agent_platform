from __future__ import annotations

import json

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from agents.application.registry import UnknownAgentRuntime
from agents.domain.runtime import AgentExecutionContext, AgentRequest
from agents.infrastructure.registry import build_default_registry
from agents.models import AgentInstallation
from assistant_core.security.ip import get_client_ip
from assistant_core.security.rate_limit import check_chat_rate_limit
from audit.services import record_audit_event
from tools.application.client_identity import (
    ServiceClientAccessError,
    ServiceClientAuthenticationError,
    authenticate_service_client_credential,
    require_service_client_installation_access,
)
from tools.application.execution import execute_tool
from tools.application.registry import UnknownToolRuntime
from tools.models import AgentToolBinding, ToolExecution

ACTION_SERVICE_CLIENT_AGENT_EXECUTED = "service_client.agent.executed"
ACTION_SERVICE_CLIENT_TOOL_EXECUTED = "service_client.tool.executed"

runtime_registry = build_default_registry()


def _json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ValidationError("invalid_json") from exc


def _rate_limit(request, key, *, limit_name="client-api"):
    result = check_chat_rate_limit(f"{limit_name}:{key}", get_client_ip(request))
    if not result.allowed:
        return JsonResponse({"error": "rate_limited", "limit": result.limit, "window_seconds": result.window_seconds}, status=429)
    return None


def _principal(request):
    limited = _rate_limit(request, "client-auth")
    if limited:
        return limited
    try:
        return authenticate_service_client_credential(request.META.get("HTTP_AUTHORIZATION", ""), request=request)
    except ServiceClientAuthenticationError as exc:
        return JsonResponse({"error": "service_client_authentication_failed", "details": exc.messages}, status=401)


def _tool_payload(tool):
    return {"id": str(tool.id), "slug": tool.slug, "name": tool.name, "category": tool.category}


def _client_payload(principal):
    client = principal.service_client
    return {
        "id": str(client.id),
        "tenant_id": client.tenant_id,
        "name": client.name,
        "slug": client.slug,
        "is_active": client.is_active,
        "credential_id": principal.credential_id,
    }


def _execution_payload(execution):
    payload = {
        "id": str(execution.id),
        "tool": _tool_payload(execution.tool_definition),
        "status": execution.status,
        "created_at": execution.created_at.isoformat(),
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
    }
    if execution.status == ToolExecution.Status.SUCCEEDED:
        payload["result"] = execution.result_payload
    if execution.status == ToolExecution.Status.FAILED:
        payload["error_code"] = execution.error_code
        payload["error_message"] = execution.error_message
    return payload


def _load_authorized_installation(installation_id, principal):
    installation = AgentInstallation.objects.select_related(
        "tenant", "project", "agent_definition", "agent_version"
    ).get(pk=installation_id, tenant_id=principal.tenant_id)
    require_service_client_installation_access(principal=principal, installation=installation)
    return installation


@require_http_methods(["GET"])
def me(request):
    principal = _principal(request)
    if isinstance(principal, JsonResponse):
        return principal
    return JsonResponse({"service_client": _client_payload(principal)})


@csrf_exempt
@require_http_methods(["POST"])
def execute_agent_installation(request, installation_id):
    limited = _rate_limit(request, f"execute-agent:{installation_id}")
    if limited:
        return limited
    principal = _principal(request)
    if isinstance(principal, JsonResponse):
        return principal
    try:
        payload = _json_body(request)
        installation = _load_authorized_installation(installation_id, principal)
    except AgentInstallation.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except ServiceClientAccessError as exc:
        return JsonResponse({"error": "not_found", "details": exc.messages}, status=404)
    except ValidationError as exc:
        return JsonResponse({"error": "invalid_request", "details": exc.messages}, status=400)

    message = payload.get("input")
    if not isinstance(message, str) or not message.strip():
        return JsonResponse({"error": "input_required"}, status=400)

    context = AgentExecutionContext(
        tenant_id=str(installation.tenant_id),
        project_id=str(installation.project_id),
        installation_id=str(installation.id),
        user_id="",
        session_id=str(payload.get("session_id") or ""),
        metadata={"agent_definition": installation.agent_definition.slug, "service_client_id": principal.service_client_id},
    )
    request_obj = AgentRequest(input=message.strip(), metadata=payload.get("metadata") or {})
    try:
        response = runtime_registry.resolve(installation.agent_version.runtime_handler).execute(context, request_obj)
    except UnknownAgentRuntime:
        return JsonResponse({"error": "unknown_runtime"}, status=500)
    record_audit_event(
        action=ACTION_SERVICE_CLIENT_AGENT_EXECUTED,
        tenant=installation.tenant,
        obj=installation,
        metadata={"service_client_id": principal.service_client_id, "installation_id": str(installation.pk), "status": response.status},
        request=request,
    )
    return JsonResponse({"output": response.output, "status": response.status, "metadata": response.metadata})


@csrf_exempt
@require_http_methods(["POST"])
def execute_installation_tool(request, installation_id, tool_slug):
    limited = _rate_limit(request, f"execute-tool:{installation_id}:{tool_slug}")
    if limited:
        return limited
    principal = _principal(request)
    if isinstance(principal, JsonResponse):
        return principal
    try:
        payload = _json_body(request)
        installation = _load_authorized_installation(installation_id, principal)
        AgentToolBinding.objects.select_related("tool_definition").get(
            agent_installation=installation,
            tenant=installation.tenant,
            project=installation.project,
            tool_definition__slug=tool_slug,
            is_enabled=True,
            tool_definition__is_active=True,
        )
        result = execute_tool(
            installation=installation,
            tool_slug=tool_slug,
            input=payload.get("input") or {},
            metadata={"service_client_id": principal.service_client_id},
            idempotency_key=payload.get("idempotency_key") or "",
            requested_by_service_client=principal.service_client,
            request=request,
        )
    except AgentInstallation.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except AgentToolBinding.DoesNotExist:
        return JsonResponse({"error": "tool_not_found"}, status=404)
    except ServiceClientAccessError as exc:
        return JsonResponse({"error": "not_found", "details": exc.messages}, status=404)
    except UnknownToolRuntime:
        return JsonResponse({"error": "unknown_tool_runtime"}, status=500)
    except ValidationError as exc:
        return JsonResponse({"error": "tool_not_executable", "details": exc.messages}, status=400)
    record_audit_event(
        action=ACTION_SERVICE_CLIENT_TOOL_EXECUTED,
        tenant=installation.tenant,
        obj=installation,
        metadata={
            "service_client_id": principal.service_client_id,
            "installation_id": str(installation.pk),
            "tool_slug": tool_slug,
            "tool_execution_id": result.metadata.get("tool_execution_id", ""),
            "status": result.status,
        },
        request=request,
    )
    return JsonResponse({"status": result.status, "tool_execution_id": result.metadata.get("tool_execution_id", "")})


@require_http_methods(["GET"])
def execution_detail(request, execution_id):
    limited = _rate_limit(request, f"status:{execution_id}")
    if limited:
        return limited
    principal = _principal(request)
    if isinstance(principal, JsonResponse):
        return principal
    try:
        execution = ToolExecution.objects.select_related(
            "tool_definition", "agent_installation", "agent_installation__project", "agent_installation__tenant"
        ).get(pk=execution_id, tenant_id=principal.tenant_id)
        require_service_client_installation_access(principal=principal, installation=execution.agent_installation)
        if str(execution.requested_by_service_client_id or "") != principal.service_client_id:
            return JsonResponse({"error": "not_found"}, status=404)
    except ToolExecution.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except ServiceClientAccessError:
        return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse(_execution_payload(execution))
