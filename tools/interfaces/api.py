from __future__ import annotations

import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from agents.models import AgentInstallation
from tenants.access import CAPABILITY_TENANT_VIEW, get_accessible_tenants, require_tenant_capability
from tools.application.execution import execute_tool
from tools.application.registry import UnknownToolRuntime
from tools.models import AgentToolBinding, ToolDefinition


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
        "is_active": tool.is_active,
    }


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
