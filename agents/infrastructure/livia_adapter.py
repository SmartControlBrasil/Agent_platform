from __future__ import annotations

import uuid

from django.core.exceptions import ObjectDoesNotExist

from agents.domain.runtime import AgentExecutionContext, AgentRequest, AgentResponse
from assistant_core.services.chat_idempotency import build_request_fingerprint, reserve_chat_request
from assistant_core.services.chat_processing import process_chat_request
from tenants.models import Tenant


class LiviaAgentAdapter:
    """Legacy compatibility adapter for the current Lívia chat pipeline."""

    def execute(self, context: AgentExecutionContext, request: AgentRequest) -> AgentResponse:
        session_id = context.session_id or str(request.metadata.get("session_id") or "").strip()
        if not session_id:
            return AgentResponse(output="", status="invalid_request", metadata={"error": "session_id_required"})

        try:
            tenant = Tenant.objects.get(pk=context.tenant_id, is_active=True)
        except (ObjectDoesNotExist, ValueError):
            return AgentResponse(output="", status="tenant_unavailable", metadata={"error": "tenant_unavailable"})

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
        )
        return AgentResponse(output=str(payload.get("reply") or ""), status="ok", metadata={"legacy_payload": payload})
