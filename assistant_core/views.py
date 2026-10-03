import json
import logging

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from agents.infrastructure.livia_configuration import LiviaConfigurationResolver
from agents.models import AgentInstallation
from tenants.origins import log_origin_block, resolve_public_tenant, validate_tenant_origin

from .security.ip import get_client_ip
from .security.rate_limit import check_chat_rate_limit
from .security.spam_guard import check_message_spam
from .services.chat_idempotency import (
    REQUEST_ID_HEADER,
    REQUEST_ID_CONFLICT,
    REQUEST_IN_PROGRESS,
    ChatIdempotencyError,
    build_request_fingerprint,
    complete_chat_request,
    fail_chat_request,
    update_completed_chat_request_response,
    parse_request_id,
    reserve_chat_request,
)
from .services.chat_processing import process_chat_request

logger = logging.getLogger(__name__)

INACTIVE_TENANT_REPLY = "Este atendimento não está disponível no momento."
LONG_MESSAGE_REPLY = "Sua mensagem está muito longa. Envie uma versão mais curta para eu conseguir te ajudar melhor."
RATE_LIMIT_REPLY = "Recebi muitas mensagens em pouco tempo. Aguarde alguns minutos e tente novamente."
SPAM_REPLY = "Não consegui processar essa mensagem. Envie uma solicitação objetiva sobre atendimento, orçamento ou suporte."
EMPTY_MESSAGE_ERROR = "message is required."
REQUEST_IN_PROGRESS_REPLY = "Sua mensagem ainda está sendo processada. Tente novamente em instantes."
REQUEST_FAILED_REPLY = "Não consegui concluir essa mensagem. Tente enviar novamente em instantes."
UNEXPECTED_ERROR_REPLY = "Não consegui responder agora. Tente novamente em instantes."


def _safe_reply_response(reply, *, status=200, code="blocked"):
    return JsonResponse({"error": code, "reply": reply}, status=status)


def _json_response(payload, *, status=200, replay=False):
    response = JsonResponse(payload, status=status)
    response["X-Livia-Idempotent-Replay"] = "true" if replay else "false"
    return response


def _active_assistant_profile(tenant):
    try:
        assistant_profile = tenant.assistant_profile
    except ObjectDoesNotExist:
        return None
    if assistant_profile is not None and not assistant_profile.is_active:
        return None
    return assistant_profile


def _resolve_livia_installation(tenant):
    return (
        AgentInstallation.objects.select_related("tenant", "project", "agent_definition", "agent_version")
        .filter(
            tenant=tenant,
            project__tenant=tenant,
            project__is_active=True,
            is_enabled=True,
            agent_definition__slug="livia",
            agent_definition__is_active=True,
            agent_version__status="active",
            agent_version__runtime_handler="livia",
        )
        .order_by("created_at", "id")
        .first()
    )


def _build_livia_effective_profile(*, tenant, installation):
    return LiviaConfigurationResolver().build_effective_profile(
        installation=installation,
        tenant=tenant,
        assistant_profile=_active_assistant_profile(tenant),
    )


def _annotate_agent_platform_observability(payload, *, installation, effective_profile):
    updated = dict(payload)
    observability = dict(updated.get("observability") or {})
    observability.update(
        {
            "agent_definition": installation.agent_definition.slug,
            "agent_version": installation.agent_version.version,
            "agent_installation_id": str(installation.id),
            "project_id": str(installation.project_id),
            "project_slug": installation.project.slug,
            "runtime_handler": installation.agent_version.runtime_handler,
            "configuration_source": "agent_installation",
            "runtime_configuration": effective_profile.runtime_configuration.as_metadata(),
        }
    )
    updated["observability"] = observability
    return updated


@csrf_exempt
def chat_api(request):
    if request.method == "OPTIONS":
        return JsonResponse({}, status=204)

    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    resolution = resolve_public_tenant(request, payload=payload)
    tenant_slug = resolution.slug
    session_id = payload.get("session_key") or payload.get("session_id")
    user_message_raw = payload.get("message")
    source_page = payload.get("source_page", "")

    if resolution.error == "tenant_required":
        return JsonResponse({"error": "tenant_required"}, status=400)
    if resolution.error == "tenant_mismatch":
        logger.warning("livia_chat_tenant_mismatch tenant_slug=%s endpoint=%s status=400 code=tenant_mismatch", tenant_slug or "unknown", request.path)
        return JsonResponse({"error": "tenant_mismatch"}, status=400)
    if resolution.error == "tenant_unavailable":
        status_code = 403 if resolution.tenant is not None else 404
        logger.info("livia_chat_tenant_unavailable tenant_slug=%s endpoint=%s status=%s code=tenant_unavailable", tenant_slug or "unknown", request.path, status_code)
        return _safe_reply_response(INACTIVE_TENANT_REPLY, status=status_code, code="tenant_unavailable")

    tenant = resolution.tenant

    origin_result = validate_tenant_origin(request, tenant)
    if not origin_result.allowed:
        log_origin_block(tenant, origin_result)
        return JsonResponse({"error": "origin_not_allowed"}, status=403)
    request.livia_validated_origin = origin_result.origin

    if not session_id:
        return JsonResponse({"error": "session_id is required."}, status=400)
    session_id = str(session_id).strip()

    if not isinstance(user_message_raw, str):
        return JsonResponse({"error": EMPTY_MESSAGE_ERROR}, status=400)

    user_message = user_message_raw.strip()
    if not user_message:
        return JsonResponse({"error": EMPTY_MESSAGE_ERROR}, status=400)

    try:
        request_id = parse_request_id(payload.get("request_id"), request.headers.get(REQUEST_ID_HEADER, ""))
    except ChatIdempotencyError as exc:
        return JsonResponse({"error": exc.code, "message": exc.message}, status=400)

    max_message_length = int(getattr(settings, "LIVIA_MAX_MESSAGE_LENGTH", 1200))
    if len(user_message) > max_message_length:
        logger.info(
            "livia_chat_message_too_long tenant_slug=%s message_length=%s max_length=%s",
            tenant_slug,
            len(user_message),
            max_message_length,
        )
        return _safe_reply_response(LONG_MESSAGE_REPLY, status=400, code="message_too_long")

    fingerprint = build_request_fingerprint(
        tenant_slug=tenant.slug,
        session_id=session_id,
        request_id=request_id,
        message=user_message,
        source_page=source_page,
    )
    reservation = reserve_chat_request(
        tenant=tenant,
        session_id=session_id,
        request_id=request_id,
        fingerprint=fingerprint,
    )

    if reservation.state == "replay":
        return _json_response(
            reservation.response_payload or {},
            status=reservation.response_status_code,
            replay=True,
        )
    if reservation.state == "conflict":
        return _json_response({"error": REQUEST_ID_CONFLICT}, status=409, replay=False)
    if reservation.state == "in_progress":
        return _json_response(
            {"error": REQUEST_IN_PROGRESS, "reply": REQUEST_IN_PROGRESS_REPLY},
            status=409,
            replay=False,
        )
    if reservation.state == "failed":
        return _json_response(
            {"error": reservation.error_code or "request_failed", "reply": REQUEST_FAILED_REPLY},
            status=409,
            replay=False,
        )

    chat_request = reservation.chat_request
    client_ip = get_client_ip(request)
    try:
        installation = _resolve_livia_installation(tenant)
        effective_profile = None
        if installation is not None:
            try:
                effective_profile = _build_livia_effective_profile(tenant=tenant, installation=installation)
            except ValidationError:
                logger.exception(
                    "livia_agent_configuration_invalid tenant_slug=%s installation_id=%s",
                    tenant.slug,
                    installation.id,
                )
                effective_profile = None

        rate_limit = check_chat_rate_limit(tenant.slug, client_ip)
        if not rate_limit.allowed:
            logger.info(
                "livia_chat_rate_limited tenant_slug=%s ip=%s limit=%s window_seconds=%s",
                tenant.slug,
                client_ip,
                rate_limit.limit,
                rate_limit.window_seconds,
            )
            response_payload = {"error": "rate_limited", "reply": RATE_LIMIT_REPLY}
            complete_chat_request(chat_request, response_payload=response_payload, status_code=429)
            return _json_response(response_payload, status=429, replay=False)

        spam_check = check_message_spam(user_message)
        if spam_check.is_spam:
            logger.info(
                "livia_chat_spam_blocked tenant_slug=%s ip=%s reason=%s message_length=%s",
                tenant.slug,
                client_ip,
                spam_check.reason,
                len(user_message),
            )
            response_payload = {"error": "spam_blocked", "reply": SPAM_REPLY}
            complete_chat_request(chat_request, response_payload=response_payload, status_code=400)
            return _json_response(response_payload, status=400, replay=False)

        response_payload = process_chat_request(
            chat_request=chat_request,
            tenant=tenant,
            session_id=session_id,
            user_message=user_message,
            source_page=source_page,
            assistant_profile_override=effective_profile,
        )
        if installation is not None and effective_profile is not None:
            response_payload = _annotate_agent_platform_observability(
                response_payload,
                installation=installation,
                effective_profile=effective_profile,
            )
            update_completed_chat_request_response(chat_request, response_payload=response_payload, status_code=200)
        return _json_response(response_payload, status=200, replay=False)
    except Exception:
        fail_chat_request(chat_request, error_code="unexpected_error")
        logger.exception(
            "livia_chat_unexpected_error tenant_slug=%s session_hash_unavailable request_id=%s",
            tenant.slug,
            request_id,
        )
        return _json_response({"error": "unexpected_error", "reply": UNEXPECTED_ERROR_REPLY}, status=500, replay=False)
