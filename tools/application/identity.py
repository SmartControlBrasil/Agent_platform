from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit_event
from tools.models import ToolExecutor, ToolExecutorCredential, ToolExecutorPairingRequest

ACTION_PAIRING_REQUESTED = "executor.pairing.requested"
ACTION_PAIRING_APPROVED = "executor.pairing.approved"
ACTION_PAIRING_REJECTED = "executor.pairing.rejected"
ACTION_EXECUTOR_CREATED = "executor.created"
ACTION_CREDENTIAL_CREATED = "executor.credential.created"
ACTION_CREDENTIAL_ROTATED = "executor.credential.rotated"
ACTION_CREDENTIAL_REVOKED = "executor.credential.revoked"
ACTION_AUTH_FAILED = "executor.auth.failed"
ACTION_HEARTBEAT = "executor.heartbeat"

PAIRING_TTL_MINUTES = 10
EXECUTOR_AUTH_SCHEME = "AgentExecutor"


@dataclass(frozen=True)
class IssuedExecutorCredential:
    credential: ToolExecutorCredential
    secret: str


@dataclass(frozen=True)
class ExecutorPrincipal:
    executor_id: str
    tenant_id: int | None
    credential_id: str
    executor_type: str
    executor: ToolExecutor
    credential: ToolExecutorCredential


class ExecutorAuthenticationError(ValidationError):
    pass


def request_pairing(*, executor_type: str, requested_name: str, request=None) -> ToolExecutorPairingRequest:
    if executor_type not in ToolExecutor.ExecutorType.values:
        raise ValidationError("Unsupported executor type.")
    name = " ".join(str(requested_name or "").split())[:160]
    if not name:
        raise ValidationError("requested_name is required.")
    pairing = ToolExecutorPairingRequest.objects.create(
        tenant=None,
        executor_type=executor_type,
        requested_name=name,
        pairing_code=_unique_pairing_code(),
        expires_at=timezone.now() + timezone.timedelta(minutes=PAIRING_TTL_MINUTES),
    )
    record_audit_event(
        action=ACTION_PAIRING_REQUESTED,
        obj=pairing,
        object_type="tools.tool_executor_pairing_request",
        object_id=str(pairing.pk),
        metadata={"pairing_id": str(pairing.pk), "executor_type": executor_type},
        request=request,
    )
    return pairing


def approve_pairing(*, pairing: ToolExecutorPairingRequest, tenant, actor=None, request=None) -> ToolExecutorPairingRequest:
    if tenant is None:
        raise ValidationError("tenant is required.")
    pairing = _expire_pairing_before_transition(pairing)
    if pairing.status != ToolExecutorPairingRequest.Status.PENDING:
        raise ValidationError("Only pending pairing requests can be approved.")
    with transaction.atomic():
        pairing = ToolExecutorPairingRequest.objects.select_for_update().get(pk=pairing.pk)
        if pairing.status != ToolExecutorPairingRequest.Status.PENDING:
            raise ValidationError("Only pending pairing requests can be approved.")
        executor = ToolExecutor.objects.create(
            tenant=tenant,
            name=pairing.requested_name,
            executor_type=pairing.executor_type,
            public_id=_unique_executor_public_id(),
            is_active=True,
        )
        pairing.tenant = tenant
        pairing.executor = executor
        pairing.status = ToolExecutorPairingRequest.Status.APPROVED
        pairing.approved_at = timezone.now()
        pairing.approved_by = actor if getattr(actor, "is_authenticated", False) else None
        pairing.save(update_fields=["tenant", "executor", "status", "approved_at", "approved_by"])
    record_audit_event(action=ACTION_EXECUTOR_CREATED, actor=actor, tenant=tenant, obj=executor, request=request)
    record_audit_event(action=ACTION_PAIRING_APPROVED, actor=actor, tenant=tenant, obj=pairing, request=request)
    return pairing


def reject_pairing(*, pairing: ToolExecutorPairingRequest, actor=None, request=None) -> ToolExecutorPairingRequest:
    pairing = _expire_pairing_before_transition(pairing)
    if pairing.status != ToolExecutorPairingRequest.Status.PENDING:
        raise ValidationError("Only pending pairing requests can be rejected.")
    with transaction.atomic():
        pairing = ToolExecutorPairingRequest.objects.select_for_update().get(pk=pairing.pk)
        if pairing.status != ToolExecutorPairingRequest.Status.PENDING:
            raise ValidationError("Only pending pairing requests can be rejected.")
        pairing.status = ToolExecutorPairingRequest.Status.REJECTED
        pairing.save(update_fields=["status"])
    record_audit_event(action=ACTION_PAIRING_REJECTED, actor=actor, tenant=pairing.tenant, obj=pairing, request=request)
    return pairing


def consume_pairing(*, pairing_id, pairing_code: str, request=None) -> IssuedExecutorCredential:
    pairing = ToolExecutorPairingRequest.objects.select_related("executor", "tenant").get(pk=pairing_id)
    pairing = _expire_pairing_before_transition(pairing)
    if pairing.status != ToolExecutorPairingRequest.Status.APPROVED:
        raise ValidationError("Pairing is not approved.")
    with transaction.atomic():
        pairing = ToolExecutorPairingRequest.objects.select_for_update().select_related("executor", "tenant").get(pk=pairing_id)
        if pairing.status != ToolExecutorPairingRequest.Status.APPROVED:
            raise ValidationError("Pairing is not approved.")
        if not secrets.compare_digest(str(pairing.pairing_code), str(pairing_code or "")):
            raise ValidationError("Invalid pairing code.")
        if pairing.executor is None:
            raise ValidationError("Pairing does not have an executor.")
        issued = create_executor_credential(executor=pairing.executor, request=request)
        pairing.status = ToolExecutorPairingRequest.Status.CONSUMED
        pairing.save(update_fields=["status"])
    return issued


def create_executor_credential(*, executor: ToolExecutor, expires_at=None, request=None) -> IssuedExecutorCredential:
    prefix = _unique_credential_prefix()
    secret = f"aep_{prefix}_{secrets.token_urlsafe(32)}"
    credential = ToolExecutorCredential.objects.create(
        executor=executor,
        credential_prefix=prefix,
        secret_hash=make_password(secret),
        expires_at=expires_at,
    )
    record_audit_event(
        action=ACTION_CREDENTIAL_CREATED,
        tenant=executor.tenant,
        obj=credential,
        metadata={"executor_id": str(executor.pk), "credential_id": str(credential.pk), "credential_prefix": prefix},
        request=request,
    )
    return IssuedExecutorCredential(credential=credential, secret=secret)


def revoke_credential(*, credential: ToolExecutorCredential, actor=None, request=None) -> ToolExecutorCredential:
    credential.is_active = False
    credential.revoked_at = timezone.now()
    credential.save(update_fields=["is_active", "revoked_at"])
    record_audit_event(action=ACTION_CREDENTIAL_REVOKED, actor=actor, tenant=credential.executor.tenant, obj=credential, request=request)
    return credential


def rotate_credential(*, credential: ToolExecutorCredential, actor=None, request=None) -> IssuedExecutorCredential:
    executor = credential.executor
    revoke_credential(credential=credential, actor=actor, request=request)
    issued = create_executor_credential(executor=executor, expires_at=credential.expires_at, request=request)
    record_audit_event(
        action=ACTION_CREDENTIAL_ROTATED,
        actor=actor,
        tenant=executor.tenant,
        obj=issued.credential,
        metadata={"executor_id": str(executor.pk), "old_credential_id": str(credential.pk), "new_credential_id": str(issued.credential.pk)},
        request=request,
    )
    return issued


def authenticate_executor_credential(raw_header: str, *, request=None) -> ExecutorPrincipal:
    credential_value = _credential_from_authorization(raw_header)
    if not credential_value:
        _record_auth_failed(reason="missing", request=request)
        raise ExecutorAuthenticationError("Executor credential is required.")
    prefix = _prefix_from_credential(credential_value)
    if not prefix:
        _record_auth_failed(reason="malformed", request=request)
        raise ExecutorAuthenticationError("Executor credential is invalid.")
    try:
        credential = ToolExecutorCredential.objects.select_related("executor", "executor__tenant").get(credential_prefix=prefix)
    except ToolExecutorCredential.DoesNotExist as exc:
        _record_auth_failed(reason="unknown_prefix", request=request)
        raise ExecutorAuthenticationError("Executor credential is invalid.") from exc
    if not credential.is_usable or not credential.executor.is_active:
        _record_auth_failed(reason="inactive", credential=credential, request=request)
        raise ExecutorAuthenticationError("Executor credential is inactive.")
    if not check_password(credential_value, credential.secret_hash):
        _record_auth_failed(reason="hash_mismatch", credential=credential, request=request)
        raise ExecutorAuthenticationError("Executor credential is invalid.")
    credential.last_used_at = timezone.now()
    credential.save(update_fields=["last_used_at"])
    return ExecutorPrincipal(
        executor_id=str(credential.executor_id),
        tenant_id=credential.executor.tenant_id,
        credential_id=str(credential.pk),
        executor_type=credential.executor.executor_type,
        executor=credential.executor,
        credential=credential,
    )


def record_executor_heartbeat(*, principal: ExecutorPrincipal, request=None) -> ToolExecutor:
    executor = principal.executor
    executor.last_seen_at = timezone.now()
    executor.save(update_fields=["last_seen_at", "updated_at"])
    record_audit_event(action=ACTION_HEARTBEAT, tenant=executor.tenant, obj=executor, metadata={"credential_id": principal.credential_id}, request=request)
    return executor


def _credential_from_authorization(raw_header: str) -> str:
    header = str(raw_header or "").strip()
    if not header:
        return ""
    parts = header.split(None, 1)
    if len(parts) != 2:
        return ""
    scheme, value = parts
    if scheme not in {EXECUTOR_AUTH_SCHEME, "Bearer"}:
        return ""
    return value.strip()


def _prefix_from_credential(value: str) -> str:
    parts = str(value or "").split("_", 2)
    if len(parts) != 3 or parts[0] != "aep" or not parts[1] or not parts[2]:
        return ""
    return parts[1]


def _expire_pairing_before_transition(pairing: ToolExecutorPairingRequest) -> ToolExecutorPairingRequest:
    pairing = ToolExecutorPairingRequest.objects.get(pk=pairing.pk)
    if pairing.status == ToolExecutorPairingRequest.Status.PENDING and pairing.is_expired:
        pairing.status = ToolExecutorPairingRequest.Status.EXPIRED
        pairing.save(update_fields=["status"])
    return pairing


def _record_auth_failed(*, reason: str, credential: ToolExecutorCredential | None = None, request=None) -> None:
    metadata: dict[str, Any] = {"reason": reason}
    tenant = None
    obj = None
    if credential is not None:
        tenant = credential.executor.tenant
        obj = credential.executor
        metadata["executor_id"] = str(credential.executor_id)
        metadata["credential_prefix"] = credential.credential_prefix
    record_audit_event(action=ACTION_AUTH_FAILED, tenant=tenant, obj=obj, object_type="tools.tool_executor", metadata=metadata, request=request)


def _unique_pairing_code() -> str:
    while True:
        code = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12].upper()
        if not ToolExecutorPairingRequest.objects.filter(pairing_code=code).exists():
            return code


def _unique_credential_prefix() -> str:
    while True:
        prefix = secrets.token_hex(8)
        if not ToolExecutorCredential.objects.filter(credential_prefix=prefix).exists():
            return prefix


def _unique_executor_public_id() -> str:
    while True:
        public_id = f"exec_{secrets.token_hex(12)}"
        if not ToolExecutor.objects.filter(public_id=public_id).exists():
            return public_id
