from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from audit.services import record_audit_event
from tools.application.identity import approve_pairing
from tools.models import ToolDefinition, ToolExecutorCapability, ToolExecutorPairingRequest

ACTION_EXECUTOR_CAPABILITY_ENABLED = "executor.capability.enabled"
DEFAULT_TENANT_EXECUTOR_TOOL_SLUGS = ("prospecting.search_google_maps",)


@dataclass(frozen=True)
class TenantPairingClaimResult:
    pairing: ToolExecutorPairingRequest
    enabled_capability_slugs: tuple[str, ...]


class TenantPairingClaimError(ValidationError):
    pass


def claim_pairing_for_tenant(
    *,
    pairing_code: str,
    tenant,
    actor=None,
    request=None,
    capability_slugs: tuple[str, ...] = DEFAULT_TENANT_EXECUTOR_TOOL_SLUGS,
) -> TenantPairingClaimResult:
    if tenant is None:
        raise TenantPairingClaimError("Tenant ativo é obrigatório para vincular executor.")
    normalized_code = _normalize_pairing_code(pairing_code)
    if not normalized_code:
        raise TenantPairingClaimError("Informe o código de pareamento.")
    try:
        pairing = ToolExecutorPairingRequest.objects.select_related("tenant", "executor").get(pairing_code=normalized_code)
    except ToolExecutorPairingRequest.DoesNotExist as exc:
        raise TenantPairingClaimError("Código de pareamento não encontrado.") from exc
    if pairing.tenant_id is not None and pairing.tenant_id != tenant.id:
        raise TenantPairingClaimError("Este código de pareamento não pertence ao tenant ativo.")
    if pairing.status == ToolExecutorPairingRequest.Status.PENDING and pairing.is_expired:
        pairing.status = ToolExecutorPairingRequest.Status.EXPIRED
        pairing.save(update_fields=["status"])
    if pairing.status != ToolExecutorPairingRequest.Status.PENDING:
        raise TenantPairingClaimError(_message_for_status(pairing.status))

    with transaction.atomic():
        approved = approve_pairing(pairing=pairing, tenant=tenant, actor=actor, request=request)
        enabled_slugs = _enable_default_capabilities(approved, capability_slugs=capability_slugs, actor=actor, request=request)
    return TenantPairingClaimResult(pairing=approved, enabled_capability_slugs=tuple(enabled_slugs))


def _normalize_pairing_code(pairing_code: str) -> str:
    return "".join(char for char in str(pairing_code or "").strip().upper() if char.isalnum())


def _message_for_status(status: str) -> str:
    messages = {
        ToolExecutorPairingRequest.Status.APPROVED: "Este código já foi aprovado e aguarda consumo pelo executor.",
        ToolExecutorPairingRequest.Status.CONSUMED: "Este código já foi consumido por um executor.",
        ToolExecutorPairingRequest.Status.REJECTED: "Este código de pareamento foi rejeitado.",
        ToolExecutorPairingRequest.Status.EXPIRED: "Este código de pareamento expirou. Gere um novo código na extensão.",
    }
    return messages.get(status, "Este código de pareamento não está disponível.")


def _enable_default_capabilities(
    pairing: ToolExecutorPairingRequest,
    *,
    capability_slugs: tuple[str, ...],
    actor=None,
    request=None,
) -> list[str]:
    if pairing.executor is None:
        raise TenantPairingClaimError("Pareamento aprovado sem executor associado.")
    enabled_slugs: list[str] = []
    tools = ToolDefinition.objects.filter(slug__in=capability_slugs, is_active=True)
    tools_by_slug = {tool.slug: tool for tool in tools}
    missing_slugs = [slug for slug in capability_slugs if slug not in tools_by_slug]
    if missing_slugs:
        raise TenantPairingClaimError("Capability padrão do executor não está disponível.")
    for slug in capability_slugs:
        tool = tools_by_slug[slug]
        capability, _created = ToolExecutorCapability.objects.get_or_create(
            executor=pairing.executor,
            tool_definition=tool,
            defaults={"is_enabled": True},
        )
        if not capability.is_enabled:
            capability.is_enabled = True
            capability.save(update_fields=["is_enabled", "updated_at"])
        enabled_slugs.append(slug)
        record_audit_event(
            action=ACTION_EXECUTOR_CAPABILITY_ENABLED,
            actor=actor,
            tenant=pairing.tenant,
            obj=capability,
            metadata={"executor_id": str(pairing.executor_id), "tool_slug": slug},
            request=request,
        )
    return enabled_slugs
