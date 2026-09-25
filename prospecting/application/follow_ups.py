from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record_audit_event
from prospecting.models import Prospect, ProspectContact, ProspectContactOutcome, ProspectFollowUp, ProspectOutreachSend

ACTION_FOLLOW_UP_CREATED = "prospecting.follow_up.created"
ACTION_FOLLOW_UP_UPDATED = "prospecting.follow_up.updated"
ACTION_FOLLOW_UP_COMPLETED = "prospecting.follow_up.completed"
ACTION_FOLLOW_UP_CANCELLED = "prospecting.follow_up.cancelled"


def _follow_up_snapshot(follow_up):
    return {
        "action_type": follow_up.action_type,
        "status": follow_up.status,
        "note": follow_up.note or "",
        "due_at": follow_up.due_at.isoformat() if follow_up.due_at else "",
        "contact_id": str(follow_up.contact_id or ""),
    }


def _validate_prospect_tenant(*, tenant, prospect):
    locked = Prospect.objects.select_for_update().get(pk=prospect.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Prospect does not belong to the tenant.")
    return locked


def _resolve_contact(*, tenant, prospect, contact):
    if contact is None:
        return None
    locked = ProspectContact.objects.select_related("prospect").get(pk=contact.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Contact does not belong to the tenant.")
    if locked.prospect_id != prospect.id:
        raise ValidationError("Contact must belong to the same prospect.")
    return locked


def _resolve_outreach_send(*, tenant, prospect, outreach_send):
    if outreach_send is None:
        return None
    locked = ProspectOutreachSend.objects.select_related("prospect").get(pk=outreach_send.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Outreach send does not belong to the tenant.")
    if locked.prospect_id != prospect.id:
        raise ValidationError("Outreach send must belong to the same prospect.")
    return locked


def _resolve_contact_outcome(*, tenant, prospect, contact_outcome):
    if contact_outcome is None:
        return None
    locked = ProspectContactOutcome.objects.select_related("prospect").get(pk=contact_outcome.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Contact outcome does not belong to the tenant.")
    if locked.prospect_id != prospect.id:
        raise ValidationError("Contact outcome must belong to the same prospect.")
    return locked


@transaction.atomic
def create_prospect_follow_up(
    *,
    tenant,
    prospect,
    action_type,
    note="",
    due_at=None,
    contact=None,
    outreach_send=None,
    contact_outcome=None,
    idempotency_key,
    actor=None,
    request=None,
):
    if not idempotency_key:
        raise ValidationError({"idempotency_key": "Idempotency key is required."})
    existing = ProspectFollowUp.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        if existing.tenant_id != tenant.id or existing.prospect_id != prospect.id:
            raise ValidationError("Idempotency key belongs to another record.")
        return existing, False

    locked_prospect = _validate_prospect_tenant(tenant=tenant, prospect=prospect)
    locked_contact = _resolve_contact(tenant=tenant, prospect=locked_prospect, contact=contact)
    locked_send = _resolve_outreach_send(tenant=tenant, prospect=locked_prospect, outreach_send=outreach_send)
    locked_outcome = _resolve_contact_outcome(tenant=tenant, prospect=locked_prospect, contact_outcome=contact_outcome)
    action = str(action_type or "").upper()
    if action not in ProspectFollowUp.ActionType.values:
        raise ValidationError({"action_type": "Invalid follow-up action type."})

    follow_up = ProspectFollowUp(
        tenant=tenant,
        prospect=locked_prospect,
        contact=locked_contact,
        outreach_send=locked_send,
        contact_outcome=locked_outcome,
        action_type=action,
        note=note or "",
        due_at=due_at,
        status=ProspectFollowUp.Status.PENDING,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        idempotency_key=idempotency_key,
    )
    try:
        follow_up.save()
    except IntegrityError:
        return ProspectFollowUp.objects.get(idempotency_key=idempotency_key), False

    record_audit_event(
        action=ACTION_FOLLOW_UP_CREATED,
        actor=actor,
        tenant=tenant,
        obj=follow_up,
        after_data=_follow_up_snapshot(follow_up),
        metadata={"prospect_id": str(locked_prospect.id), "follow_up_id": str(follow_up.id)},
        request=request,
    )
    return follow_up, True


@transaction.atomic
def update_prospect_follow_up(
    *,
    tenant,
    follow_up,
    action_type,
    note="",
    due_at=None,
    contact=None,
    actor=None,
    request=None,
):
    locked = ProspectFollowUp.objects.select_for_update().select_related("prospect").get(pk=follow_up.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Follow-up does not belong to the tenant.")
    if locked.status != ProspectFollowUp.Status.PENDING:
        raise ValidationError("Only pending follow-ups can be edited.")
    before = _follow_up_snapshot(locked)
    locked_contact = _resolve_contact(tenant=tenant, prospect=locked.prospect, contact=contact)
    locked.action_type = str(action_type or "").upper()
    if locked.action_type not in ProspectFollowUp.ActionType.values:
        raise ValidationError({"action_type": "Invalid follow-up action type."})
    locked.note = note or ""
    locked.due_at = due_at
    locked.contact = locked_contact
    locked.full_clean()
    after = _follow_up_snapshot(locked)
    if before == after:
        return locked, False
    locked.save(update_fields=["action_type", "note", "due_at", "contact", "updated_at"])
    record_audit_event(
        action=ACTION_FOLLOW_UP_UPDATED,
        actor=actor,
        tenant=tenant,
        obj=locked,
        before_data=before,
        after_data=after,
        metadata={"prospect_id": str(locked.prospect_id), "follow_up_id": str(locked.id)},
        request=request,
    )
    return locked, True


@transaction.atomic
def complete_prospect_follow_up(*, tenant, follow_up, actor=None, request=None):
    locked = ProspectFollowUp.objects.select_for_update().select_related("prospect").get(pk=follow_up.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Follow-up does not belong to the tenant.")
    if locked.status != ProspectFollowUp.Status.PENDING:
        raise ValidationError("Only pending follow-ups can be completed.")
    before_status = locked.status
    locked.status = ProspectFollowUp.Status.COMPLETED
    locked.completed_at = timezone.now()
    locked.completed_by = actor if getattr(actor, "is_authenticated", False) else None
    locked.save(update_fields=["status", "completed_at", "completed_by", "updated_at"])
    record_audit_event(
        action=ACTION_FOLLOW_UP_COMPLETED,
        actor=actor,
        tenant=tenant,
        obj=locked,
        before_data={"status": before_status},
        after_data={"status": locked.status},
        metadata={"prospect_id": str(locked.prospect_id), "follow_up_id": str(locked.id)},
        request=request,
    )
    return locked


@transaction.atomic
def cancel_prospect_follow_up(*, tenant, follow_up, actor=None, request=None):
    locked = ProspectFollowUp.objects.select_for_update().select_related("prospect").get(pk=follow_up.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Follow-up does not belong to the tenant.")
    if locked.status != ProspectFollowUp.Status.PENDING:
        raise ValidationError("Only pending follow-ups can be cancelled.")
    before_status = locked.status
    locked.status = ProspectFollowUp.Status.CANCELLED
    locked.completed_at = timezone.now()
    locked.completed_by = actor if getattr(actor, "is_authenticated", False) else None
    locked.save(update_fields=["status", "completed_at", "completed_by", "updated_at"])
    record_audit_event(
        action=ACTION_FOLLOW_UP_CANCELLED,
        actor=actor,
        tenant=tenant,
        obj=locked,
        before_data={"status": before_status},
        after_data={"status": locked.status},
        metadata={"prospect_id": str(locked.prospect_id), "follow_up_id": str(locked.id)},
        request=request,
    )
    return locked
