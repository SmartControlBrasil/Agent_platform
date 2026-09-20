from __future__ import annotations

import uuid

from django.core.exceptions import ObjectDoesNotExist, ValidationError

from agents.domain.runtime import AgentExecutionContext, AgentRequest, AgentResponse
from agents.infrastructure.livia_configuration import LiviaConfigurationResolver
from agents.models import AgentInstallation
from assistant_core.services.chat_idempotency import build_request_fingerprint, reserve_chat_request
from assistant_core.services.chat_processing import process_chat_request
from tenants.models import Tenant


class LiviaAgentAdapter:
    """Legacy compatibility adapter for the current Lívia chat pipeline."""

    def __init__(self, configuration_resolver: LiviaConfigurationResolver | None = None):
        self.configuration_resolver = configuration_resolver or LiviaConfigurationResolver()

    def execute(self, context: AgentExecutionContext, request: AgentRequest) -> AgentResponse:
        session_id = context.session_id or str(request.metadata.get("session_id") or "").strip()
        if not session_id:
            return AgentResponse(output="", status="invalid_request", metadata={"error": "session_id_required"})

        try:
            tenant = Tenant.objects.get(pk=context.tenant_id, is_active=True)
        except (ObjectDoesNotExist, ValueError):
            return AgentResponse(output="", status="tenant_unavailable", metadata={"error": "tenant_unavailable"})

        try:
            installation = self._load_installation(context=context, tenant=tenant)
            assistant_profile = _active_assistant_profile(tenant)
            effective_profile = self.configuration_resolver.build_effective_profile(
                installation=installation,
                tenant=tenant,
                assistant_profile=assistant_profile,
            )
        except (ObjectDoesNotExist, ValueError):
            return AgentResponse(output="", status="installation_unavailable", metadata={"error": "installation_unavailable"})
        except ValidationError as exc:
            return AgentResponse(output="", status="invalid_configuration", metadata={"error": "invalid_configuration", "details": exc.messages})

        request_id = request.metadata.get("request_id") or uuid.uuid4()
        source_page = str(request.metadata.get("source_page") or "")
        fingerprint = build_request_fingerprint(
            tenant_slug=tenant.slug,
            session_id=session_id,
            request_id=request_id,
            message=request.input,
            source_page=source_page,
        )
        reservation = reserve_chat_request(
            tenant=tenant,
            session_id=session_id,
            request_id=request_id,
            fingerprint=fingerprint,
        )
        if reservation.state != "process":
            payload = reservation.response_payload or {}
            return AgentResponse(
                output=str(payload.get("reply") or ""),
                status=reservation.state,
                metadata={"legacy_payload": payload},
            )

        payload = process_chat_request(
            chat_request=reservation.chat_request,
            tenant=tenant,
            session_id=session_id,
            user_message=request.input,
            source_page=source_page,
            assistant_profile_override=effective_profile,
        )
        return AgentResponse(
            output=str(payload.get("reply") or ""),
            status="ok",
            metadata={
                "legacy_payload": payload,
                "runtime_configuration": effective_profile.runtime_configuration.as_metadata(),
                "installation_id": str(installation.id),
                "project_id": str(installation.project_id),
            },
        )

    def _load_installation(self, *, context: AgentExecutionContext, tenant: Tenant) -> AgentInstallation:
        return AgentInstallation.objects.select_related("tenant", "project", "agent_definition", "agent_version").get(
            pk=context.installation_id,
            tenant=tenant,
            project_id=context.project_id,
            is_enabled=True,
            agent_definition__slug="livia",
        )


def _active_assistant_profile(tenant):
    try:
        assistant_profile = tenant.assistant_profile
    except ObjectDoesNotExist:
        return None
    if assistant_profile is not None and not assistant_profile.is_active:
        return None
    return assistant_profile
