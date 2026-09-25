from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from audit.services import record_audit_event
from prospecting.application.follow_ups import create_prospect_follow_up
from prospecting.models import (
    Prospect,
    ProspectActivity,
    ProspectContact,
    ProspectContactOutcome,
    ProspectOutreachSend,
)

ACTION_CONTACT_OUTCOME_CREATED = "prospecting.contact_outcome.created"


def _outcome_snapshot(outcome):
    return {
        "outcome": outcome.outcome,
        "note": outcome.note or "",
        "occurred_at": outcome.occurred_at.isoformat() if outcome.occurred_at else "",
        "contact_id": str(outcome.contact_id),
        "outreach_send_id": str(outcome.outreach_send_id or ""),
    }


def _activity_type_for_outcome(outcome_value):
    if outcome_value in {
        ProspectContactOutcome.Outcome.INTERESTED,
        ProspectContactOutcome.Outcome.CALLBACK_REQUESTED,
        ProspectContactOutcome.Outcome.NOT_INTERESTED,
        ProspectContactOutcome.Outcome.OTHER,
    }:
        return ProspectActivity.ActivityType.REPLY_RECEIVED
    return ProspectActivity.ActivityType.NOTE


def _activity_note_for_outcome(*, outcome_value, note):
    label = dict(ProspectContactOutcome.Outcome.choices).get(outcome_value, outcome_value)
    lines = [f"Resultado: {label}"]
    if note:
        lines.append(note)
    return "\n".join(lines)


def _validate_prospect_tenant(*, tenant, prospect):
    locked = Prospect.objects.select_for_update().get(pk=prospect.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Prospect does not belong to the tenant.")
    return locked


def _resolve_contact(*, tenant, prospect, contact):
    if contact is None:
        raise ValidationError({"contact": "Contact is required."})
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


def _create_activity_for_outcome(*, tenant, outcome, actor):
    if outcome.prospect_activity_id:
        return outcome.prospect_activity
    activity = ProspectActivity(
        tenant=tenant,
        prospect=outcome.prospect,
        contact=outcome.contact,
        activity_type=_activity_type_for_outcome(outcome.outcome),
        note=_activity_note_for_outcome(outcome_value=outcome.outcome, note=outcome.note),
        occurred_at=outcome.occurred_at,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    activity.full_clean()
    activity.save()
    outcome.prospect_activity = activity
    outcome.save(update_fields=["prospect_activity", "updated_at"])
    return activity


@transaction.atomic
def record_prospect_contact_outcome(
    *,
    tenant,
    prospect,
    contact,
    outcome,
    note="",
    occurred_at=None,
    outreach_send=None,
    idempotency_key,
    actor=None,
    request=None,
    follow_up_action_type=None,
    follow_up_due_at=None,
    follow_up_note="",
    follow_up_idempotency_key=None,
):
    if not idempotency_key:
        raise ValidationError({"idempotency_key": "Idempotency key is required."})
    existing = ProspectContactOutcome.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        if existing.tenant_id != tenant.id or existing.prospect_id != prospect.id:
            raise ValidationError("Idempotency key belongs to another record.")
        return existing, False, None

    locked_prospect = _validate_prospect_tenant(tenant=tenant, prospect=prospect)
    locked_contact = _resolve_contact(tenant=tenant, prospect=locked_prospect, contact=contact)
    locked_send = _resolve_outreach_send(tenant=tenant, prospect=locked_prospect, outreach_send=outreach_send)
    outcome_value = str(outcome or "").upper()
    if outcome_value not in ProspectContactOutcome.Outcome.values:
        raise ValidationError({"outcome": "Invalid contact outcome."})

    from django.utils import timezone

    when = occurred_at or timezone.now()
    record = ProspectContactOutcome(
        tenant=tenant,
        prospect=locked_prospect,
        contact=locked_contact,
        outreach_send=locked_send,
        outcome=outcome_value,
        note=note or "",
        occurred_at=when,
        recorded_by=actor if getattr(actor, "is_authenticated", False) else None,
        idempotency_key=idempotency_key,
    )
    try:
        record.save()
    except IntegrityError:
        existing = ProspectContactOutcome.objects.get(idempotency_key=idempotency_key)
        return existing, False, None

    _create_activity_for_outcome(tenant=tenant, outcome=record, actor=actor)
    record_audit_event(
        action=ACTION_CONTACT_OUTCOME_CREATED,
        actor=actor,
        tenant=tenant,
        obj=record,
        after_data=_outcome_snapshot(record),
        metadata={
            "prospect_id": str(locked_prospect.id),
            "outcome_id": str(record.id),
            "activity_id": str(record.prospect_activity_id or ""),
        },
        request=request,
    )

    follow_up = None
    if follow_up_action_type:
        follow_up, _ = create_prospect_follow_up(
            tenant=tenant,
            prospect=locked_prospect,
            action_type=follow_up_action_type,
            note=follow_up_note or "",
            due_at=follow_up_due_at,
            contact=locked_contact,
            outreach_send=locked_send,
            contact_outcome=record,
            idempotency_key=follow_up_idempotency_key or f"{idempotency_key}:follow_up",
            actor=actor,
            request=request,
        )
    return record, True, follow_up
