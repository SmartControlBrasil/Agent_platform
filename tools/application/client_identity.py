from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit_event
from tools.models import ServiceClient, ServiceClientAgentAccess, ServiceClientCredential

ACTION_SERVICE_CLIENT_CREATED = "service_client.created"
ACTION_SERVICE_CLIENT_CREDENTIAL_CREATED = "service_client.credential.created"
ACTION_SERVICE_CLIENT_CREDENTIAL_ROTATED = "service_client.credential.rotated"
ACTION_SERVICE_CLIENT_CREDENTIAL_REVOKED = "service_client.credential.revoked"
ACTION_SERVICE_CLIENT_AUTH_FAILED = "service_client.auth.failed"
ACTION_SERVICE_CLIENT_ACCESS_GRANTED = "service_client.access.granted"
ACTION_SERVICE_CLIENT_ACCESS_REVOKED = "service_client.access.revoked"

SERVICE_CLIENT_AUTH_SCHEME = "AgentClient"


@dataclass(frozen=True)
class IssuedServiceClientCredential:
    credential: ServiceClientCredential
    secret: str


@dataclass(frozen=True)
class ServiceClientPrincipal:
    service_client_id: str
    tenant_id: int
    credential_id: str
    service_client: ServiceClient
    credential: ServiceClientCredential


class ServiceClientAuthenticationError(ValidationError):
    pass


class ServiceClientAccessError(ValidationError):
    pass


def create_service_client(*, tenant, name: str, slug: str, is_active: bool = True, actor=None, request=None) -> ServiceClient:
    client = ServiceClient.objects.create(
        tenant=tenant,
        name=" ".join(str(name or "").split())[:160],
        slug=str(slug or "").strip(),
        is_active=is_active,
    )
    record_audit_event(
        action=ACTION_SERVICE_CLIENT_CREATED,
        actor=actor,
        tenant=tenant,
        obj=client,
        metadata={"service_client_id": str(client.pk), "slug": client.slug},
        request=request,
    )
    return client


def create_service_client_credential(*, service_client: ServiceClient, expires_at=None, actor=None, request=None) -> IssuedServiceClientCredential:
    prefix = _unique_credential_prefix()
    secret = f"apc_{prefix}_{secrets.token_urlsafe(32)}"
    credential = ServiceClientCredential.objects.create(
        service_client=service_client,
        credential_prefix=prefix,
        secret_hash=make_password(secret),
        expires_at=expires_at,
    )
    record_audit_event(
        action=ACTION_SERVICE_CLIENT_CREDENTIAL_CREATED,
        actor=actor,
        tenant=service_client.tenant,
        obj=credential,
        metadata={"service_client_id": str(service_client.pk), "credential_id": str(credential.pk), "credential_prefix": prefix},
        request=request,
    )
    return IssuedServiceClientCredential(credential=credential, secret=secret)


def revoke_service_client_credential(*, credential: ServiceClientCredential, actor=None, request=None) -> ServiceClientCredential:
    credential.is_active = False
    credential.revoked_at = timezone.now()
    credential.save(update_fields=["is_active", "revoked_at"])
    record_audit_event(
        action=ACTION_SERVICE_CLIENT_CREDENTIAL_REVOKED,
        actor=actor,
        tenant=credential.service_client.tenant,
        obj=credential,
        metadata={"service_client_id": str(credential.service_client_id), "credential_id": str(credential.pk)},
        request=request,
    )
    return credential


def rotate_service_client_credential(*, credential: ServiceClientCredential, actor=None, request=None) -> IssuedServiceClientCredential:
    service_client = credential.service_client
    revoke_service_client_credential(credential=credential, actor=actor, request=request)
    issued = create_service_client_credential(service_client=service_client, expires_at=credential.expires_at, actor=actor, request=request)
    record_audit_event(
        action=ACTION_SERVICE_CLIENT_CREDENTIAL_ROTATED,
        actor=actor,
        tenant=service_client.tenant,
        obj=issued.credential,
        metadata={
            "service_client_id": str(service_client.pk),
            "old_credential_id": str(credential.pk),
            "new_credential_id": str(issued.credential.pk),
        },
        request=request,
    )
    return issued


def authenticate_service_client_credential(raw_header: str, *, request=None) -> ServiceClientPrincipal:
    credential_value = _credential_from_authorization(raw_header)
    if not credential_value:
        _record_auth_failed(reason="missing", request=request)
        raise ServiceClientAuthenticationError("Service client credential is required.")
    prefix = _prefix_from_credential(credential_value)
    if not prefix:
        _record_auth_failed(reason="malformed", request=request)
        raise ServiceClientAuthenticationError("Service client credential is invalid.")
    try:
        credential = ServiceClientCredential.objects.select_related("service_client", "service_client__tenant").get(
            credential_prefix=prefix
        )
    except ServiceClientCredential.DoesNotExist as exc:
        _record_auth_failed(reason="unknown_prefix", request=request)
        raise ServiceClientAuthenticationError("Service client credential is invalid.") from exc
    if not credential.is_usable or not credential.service_client.is_active:
        _record_auth_failed(reason="inactive", credential=credential, request=request)
        raise ServiceClientAuthenticationError("Service client credential is inactive.")
    if not check_password(credential_value, credential.secret_hash):
        _record_auth_failed(reason="hash_mismatch", credential=credential, request=request)
        raise ServiceClientAuthenticationError("Service client credential is invalid.")
    credential.last_used_at = timezone.now()
    credential.save(update_fields=["last_used_at"])
    return ServiceClientPrincipal(
        service_client_id=str(credential.service_client_id),
        tenant_id=credential.service_client.tenant_id,
        credential_id=str(credential.pk),
        service_client=credential.service_client,
        credential=credential,
    )


def grant_service_client_access(*, service_client: ServiceClient, installation, can_execute: bool = True, actor=None, request=None) -> ServiceClientAgentAccess:
    if service_client.tenant_id != installation.tenant_id:
        raise ValidationError("Service client and installation must belong to the same tenant.")
    access, created = ServiceClientAgentAccess.objects.get_or_create(
        service_client=service_client,
        agent_installation=installation,
        defaults={"tenant": service_client.tenant, "can_execute": can_execute, "is_active": True},
    )
    if not created:
        access.tenant = service_client.tenant
        access.can_execute = can_execute
        access.is_active = True
        access.save(update_fields=["tenant", "can_execute", "is_active", "updated_at"])
    record_audit_event(
        action=ACTION_SERVICE_CLIENT_ACCESS_GRANTED,
        actor=actor,
        tenant=service_client.tenant,
        obj=access,
        metadata={"service_client_id": str(service_client.pk), "installation_id": str(installation.pk), "created": created},
        request=request,
    )
    return access


def revoke_service_client_access(*, access: ServiceClientAgentAccess, actor=None, request=None) -> ServiceClientAgentAccess:
    access.is_active = False
    access.can_execute = False
    access.save(update_fields=["is_active", "can_execute", "updated_at"])
    record_audit_event(
        action=ACTION_SERVICE_CLIENT_ACCESS_REVOKED,
        actor=actor,
        tenant=access.tenant,
        obj=access,
        metadata={"service_client_id": str(access.service_client_id), "installation_id": str(access.agent_installation_id)},
        request=request,
    )
    return access


def require_service_client_installation_access(*, principal: ServiceClientPrincipal, installation) -> ServiceClientAgentAccess:
    if installation.tenant_id != principal.tenant_id:
        raise ServiceClientAccessError("Installation not found.")
    if not installation.is_enabled:
        raise ServiceClientAccessError("Installation is disabled.")
    if not installation.project.is_active or not installation.tenant.is_active:
        raise ServiceClientAccessError("Installation is unavailable.")
    try:
        return ServiceClientAgentAccess.objects.select_related("service_client", "agent_installation").get(
            service_client=principal.service_client,
            agent_installation=installation,
            tenant_id=principal.tenant_id,
            is_active=True,
            can_execute=True,
        )
    except ServiceClientAgentAccess.DoesNotExist as exc:
        raise ServiceClientAccessError("Service client is not authorized for this installation.") from exc


def _credential_from_authorization(raw_header: str) -> str:
    header = str(raw_header or "").strip()
    if not header:
        return ""
    parts = header.split(None, 1)
    if len(parts) != 2:
        return ""
    scheme, value = parts
    if scheme != SERVICE_CLIENT_AUTH_SCHEME:
        return ""
    return value.strip()


def _prefix_from_credential(value: str) -> str:
    parts = str(value or "").split("_", 2)
    if len(parts) != 3 or parts[0] != "apc" or not parts[1] or not parts[2]:
        return ""
    return parts[1]


def _unique_credential_prefix() -> str:
    while True:
        prefix = secrets.token_hex(8)
        if not ServiceClientCredential.objects.filter(credential_prefix=prefix).exists():
            return prefix


def _record_auth_failed(*, reason: str, credential: ServiceClientCredential | None = None, request=None) -> None:
    metadata: dict[str, Any] = {"reason": reason}
    tenant = None
    obj = None
    if credential is not None:
        tenant = credential.service_client.tenant
        obj = credential.service_client
        metadata["service_client_id"] = str(credential.service_client_id)
        metadata["credential_prefix"] = credential.credential_prefix
    record_audit_event(action=ACTION_SERVICE_CLIENT_AUTH_FAILED, tenant=tenant, obj=obj, object_type="tools.service_client", metadata=metadata, request=request)
