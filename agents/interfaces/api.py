from __future__ import annotations

import json

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from agents.application.registry import UnknownAgentRuntime
from agents.domain.runtime import AgentExecutionContext, AgentRequest
from agents.infrastructure.registry import build_default_registry
from agents.models import AgentDefinition, AgentInstallation
from projects.models import Project
from tenants.access import get_accessible_tenants, require_tenant_capability, CAPABILITY_TENANT_VIEW

runtime_registry = build_default_registry()


def _json_auth_required(request):
    if not getattr(request.user, "is_authenticated", False):
        return JsonResponse({"error": "authentication_required"}, status=401)
    return None


def _accessible_tenant_ids(user):
    return list(get_accessible_tenants(user).values_list("id", flat=True))


def _tenant_filter(request):
    tenant_ids = _accessible_tenant_ids(request.user)
    if not tenant_ids:
        raise PermissionDenied
    tenant_param = request.GET.get("tenant")
    if tenant_param:
        try:
            tenant_id = int(tenant_param)
        except (TypeError, ValueError):
            raise PermissionDenied
        if tenant_id not in tenant_ids:
            raise PermissionDenied
        return [tenant_id]
    return tenant_ids


def _project_payload(project):
    return {
        "id": str(project.id),
        "tenant_id": project.tenant_id,
        "name": project.name,
        "slug": project.slug,
        "description": project.description,
        "is_active": project.is_active,
    }


def _agent_payload(agent):
    active_version = next((version for version in agent.versions.all() if version.status == "active"), None)
    return {
        "id": str(agent.id),
        "slug": agent.slug,
        "name": agent.name,
        "description": agent.description,
        "agent_type": agent.agent_type,
        "is_active": agent.is_active,
        "active_version": active_version.version if active_version else "",
    }


def _installation_payload(installation):
    return {
        "id": str(installation.id),
        "tenant_id": installation.tenant_id,
        "project_id": str(installation.project_id),
        "agent_definition_id": str(installation.agent_definition_id),
        "agent_version_id": str(installation.agent_version_id),
        "name": installation.name,
        "is_enabled": installation.is_enabled,
    }


@require_http_methods(["GET"])
def health(request):
    return JsonResponse({"status": "ok", "service": getattr(settings, "SERVICE_NAME", "agent_platform")})


@require_http_methods(["GET"])
def agents_list(request):
    auth = _json_auth_required(request)
    if auth:
        return auth
    agents = AgentDefinition.objects.prefetch_related("versions").filter(is_active=True).order_by("slug")
    return JsonResponse({"results": [_agent_payload(agent) for agent in agents]})


@require_http_methods(["GET"])
def projects_list(request):
    auth = _json_auth_required(request)
    if auth:
        return auth
    try:
        tenant_ids = _tenant_filter(request)
    except PermissionDenied:
        return JsonResponse({"error": "forbidden"}, status=403)
    projects = Project.objects.filter(tenant_id__in=tenant_ids).select_related("tenant").order_by("tenant__name", "name")
    return JsonResponse({"results": [_project_payload(project) for project in projects]})


@require_http_methods(["GET"])
def installations_list(request):
    auth = _json_auth_required(request)
    if auth:
        return auth
    try:
        tenant_ids = _tenant_filter(request)
    except PermissionDenied:
        return JsonResponse({"error": "forbidden"}, status=403)
    installations = (
        AgentInstallation.objects.filter(tenant_id__in=tenant_ids)
        .select_related("tenant", "project", "agent_definition", "agent_version")
        .order_by("tenant__name", "project__name", "name")
    )
    return JsonResponse({"results": [_installation_payload(installation) for installation in installations]})


@require_http_methods(["POST"])
def execute_installation(request, installation_id):
    auth = _json_auth_required(request)
    if auth:
        return auth
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid_json"}, status=400)

    try:
        installation = (
            AgentInstallation.objects.select_related("tenant", "project", "agent_definition", "agent_version")
            .get(pk=installation_id, tenant_id__in=_accessible_tenant_ids(request.user), is_enabled=True)
        )
        require_tenant_capability(request.user, installation.tenant, CAPABILITY_TENANT_VIEW)
    except AgentInstallation.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except PermissionDenied:
        return JsonResponse({"error": "forbidden"}, status=403)

    message = payload.get("input")
    if not isinstance(message, str) or not message.strip():
        return JsonResponse({"error": "input_required"}, status=400)

    context = AgentExecutionContext(
        tenant_id=str(installation.tenant_id),
        project_id=str(installation.project_id),
        installation_id=str(installation.id),
        user_id=str(request.user.pk),
        session_id=str(payload.get("session_id") or ""),
        metadata={"agent_definition": installation.agent_definition.slug},
    )
    request_obj = AgentRequest(input=message.strip(), metadata=payload.get("metadata") or {})
    try:
        response = runtime_registry.resolve(installation.agent_version.runtime_handler).execute(context, request_obj)
    except UnknownAgentRuntime:
        return JsonResponse({"error": "unknown_runtime"}, status=500)

    return JsonResponse({"output": response.output, "status": response.status, "metadata": response.metadata})
