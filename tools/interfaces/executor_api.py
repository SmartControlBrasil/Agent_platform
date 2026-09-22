from __future__ import annotations

import json

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from assistant_core.security.ip import get_client_ip
from assistant_core.security.rate_limit import check_chat_rate_limit
from tools.application.identity import (
    ExecutorAuthenticationError,
    authenticate_executor_credential,
    consume_pairing,
    record_executor_heartbeat,
    request_pairing,
)
from tools.application.lifecycle import (
    claim_tool_execution,
    complete_tool_execution,
    fail_tool_execution,
)
from tools.models import ToolExecution, ToolExecutor, ToolExecutorCapability, ToolExecutorPairingRequest


def _json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise ValidationError("invalid_json") from exc


def _rate_limit(request, key, *, limit_name="executor-api"):
    result = check_chat_rate_limit(f"{limit_name}:{key}", get_client_ip(request))
    if not result.allowed:
        return JsonResponse({"error": "rate_limited", "limit": result.limit, "window_seconds": result.window_seconds}, status=429)
    return None


def _principal(request):
    limited = _rate_limit(request, "executor-auth")
    if limited:
        return limited
    try:
        return authenticate_executor_credential(request.META.get("HTTP_AUTHORIZATION", ""), request=request)
    except ExecutorAuthenticationError as exc:
        return JsonResponse({"error": "executor_authentication_failed", "details": exc.messages}, status=401)


def _tool_payload(tool):
    return {"id": str(tool.id), "slug": tool.slug, "name": tool.name, "category": tool.category}


def _execution_payload(execution):
    return {
        "id": str(execution.id),
        "tenant_id": execution.tenant_id,
        "project_id": str(execution.project_id),
        "tool_definition": _tool_payload(execution.tool_definition),
        "status": execution.status,
        "request_payload": execution.request_payload,
        "result_payload": execution.result_payload,
        "error_code": execution.error_code,
        "error_message": execution.error_message,
        "created_at": execution.created_at.isoformat(),
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
        "expires_at": execution.expires_at.isoformat() if execution.expires_at else None,
    }


def _executor_payload(executor, credential_id=""):
    capabilities = executor.capabilities.filter(is_enabled=True, tool_definition__is_active=True).select_related("tool_definition")
    return {
        "id": str(executor.id),
        "public_id": executor.public_id,
        "tenant_id": executor.tenant_id,
        "name": executor.name,
        "executor_type": executor.executor_type,
        "is_active": executor.is_active,
        "last_seen_at": executor.last_seen_at.isoformat() if executor.last_seen_at else None,
        "credential_id": credential_id,
        "capabilities": [_tool_payload(capability.tool_definition) for capability in capabilities],
    }


@csrf_exempt
@require_http_methods(["POST"])
def pairing_request(request):
    limited = _rate_limit(request, "pairing-request")
    if limited:
        return limited
    try:
        payload = _json_body(request)
        pairing = request_pairing(
            executor_type=payload.get("executor_type") or ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            requested_name=payload.get("requested_name") or "Browser extension",
            request=request,
        )
    except ValidationError as exc:
        return JsonResponse({"error": "pairing_request_rejected", "details": exc.messages}, status=400)
    return JsonResponse(
        {
            "id": str(pairing.id),
            "pairing_code": pairing.pairing_code,
            "status": pairing.status,
            "expires_at": pairing.expires_at.isoformat(),
        },
        status=201,
    )


@require_http_methods(["GET"])
def pairing_status(request, pairing_id):
    limited = _rate_limit(request, f"pairing-status:{pairing_id}")
    if limited:
        return limited
    try:
        pairing = ToolExecutorPairingRequest.objects.select_related("executor").get(pk=pairing_id)
        if pairing.status == ToolExecutorPairingRequest.Status.PENDING and pairing.is_expired:
            pairing.status = ToolExecutorPairingRequest.Status.EXPIRED
            pairing.save(update_fields=["status"])
    except ToolExecutorPairingRequest.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    return JsonResponse(
        {
            "id": str(pairing.id),
            "status": pairing.status,
            "executor_id": str(pairing.executor_id or ""),
            "expires_at": pairing.expires_at.isoformat(),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
def pairing_consume(request, pairing_id):
    limited = _rate_limit(request, f"pairing-consume:{pairing_id}")
    if limited:
        return limited
    try:
        payload = _json_body(request)
        issued = consume_pairing(pairing_id=pairing_id, pairing_code=payload.get("pairing_code") or "", request=request)
    except ToolExecutorPairingRequest.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except ValidationError as exc:
        return JsonResponse({"error": "pairing_consume_rejected", "details": exc.messages}, status=400)
    return JsonResponse(
        {
            "credential": issued.secret,
            "credential_prefix": issued.credential.credential_prefix,
            "credential_id": str(issued.credential.id),
            "executor": _executor_payload(issued.credential.executor, credential_id=str(issued.credential.id)),
        }
    )


@require_http_methods(["GET"])
def me(request):
    principal = _principal(request)
    if isinstance(principal, JsonResponse):
        return principal
    return JsonResponse({"executor": _executor_payload(principal.executor, credential_id=principal.credential_id)})


@csrf_exempt
@require_http_methods(["POST"])
def heartbeat(request):
    principal = _principal(request)
    if isinstance(principal, JsonResponse):
        return principal
    executor = record_executor_heartbeat(principal=principal, request=request)
    return JsonResponse({"executor": _executor_payload(executor, credential_id=principal.credential_id)})


@require_http_methods(["GET"])
def execution_queue(request):
    principal = _principal(request)
    if isinstance(principal, JsonResponse):
        return principal
    tool_ids = ToolExecutorCapability.objects.filter(
        executor=principal.executor, is_enabled=True, tool_definition__is_active=True
    ).values_list("tool_definition_id", flat=True)
    executions = ToolExecution.objects.filter(
        tenant_id=principal.tenant_id,
        execution_mode="DELEGATED",
        status=ToolExecution.Status.DISPATCHED,
        tool_definition_id__in=tool_ids,
    ).select_related("tool_definition", "project")
    executions = executions.filter(expires_at__isnull=True) | executions.filter(expires_at__gt=timezone.now())
    return JsonResponse({"results": [_execution_payload(execution) for execution in executions.order_by("created_at")[:100]]})


def _execution_for_principal(execution_id, principal):
    return ToolExecution.objects.select_related("tool_definition").get(pk=execution_id, tenant_id=principal.tenant_id)


@csrf_exempt
@require_http_methods(["POST"])
def claim_execution(request, execution_id):
    principal = _principal(request)
    if isinstance(principal, JsonResponse):
        return principal
    try:
        execution = _execution_for_principal(execution_id, principal)
        execution = claim_tool_execution(executor=principal.executor, execution=execution, request=request)
    except ToolExecution.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except ValidationError as exc:
        return JsonResponse({"error": "claim_rejected", "details": exc.messages}, status=400)
    return JsonResponse(_execution_payload(execution))


@csrf_exempt
@require_http_methods(["POST"])
def complete_execution(request, execution_id):
    principal = _principal(request)
    if isinstance(principal, JsonResponse):
        return principal
    try:
        payload = _json_body(request)
        execution = _execution_for_principal(execution_id, principal)
        execution = complete_tool_execution(
            executor=principal.executor, execution=execution, result=payload.get("result") or {}, request=request
        )
    except ToolExecution.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except ValidationError as exc:
        return JsonResponse({"error": "completion_rejected", "details": exc.messages}, status=400)
    return JsonResponse(_execution_payload(execution))


@csrf_exempt
@require_http_methods(["POST"])
def fail_execution(request, execution_id):
    principal = _principal(request)
    if isinstance(principal, JsonResponse):
        return principal
    try:
        payload = _json_body(request)
        execution = _execution_for_principal(execution_id, principal)
        execution = fail_tool_execution(
            executor=principal.executor,
            execution=execution,
            error_code=payload.get("error_code") or "executor_failed",
            error_message=payload.get("error_message") or "",
            request=request,
        )
    except ToolExecution.DoesNotExist:
        return JsonResponse({"error": "not_found"}, status=404)
    except ValidationError as exc:
        return JsonResponse({"error": "failure_rejected", "details": exc.messages}, status=400)
    return JsonResponse(_execution_payload(execution))
