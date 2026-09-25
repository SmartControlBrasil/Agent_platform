from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit_event
from prospecting.models import Prospect, ProspectActivity, ProspectContact

ACTION_PROSPECT_ACTIVITY_CREATED = "prospecting.activity.created"
ACTION_PROSPECT_ACTIVITY_UPDATED = "prospecting.activity.updated"
ACTION_PROSPECT_ACTIVITY_DELETED = "prospecting.activity.deleted"


def _activity_snapshot(activity):
    return {
        "activity_type": activity.activity_type,
        "note": activity.note,
        "occurred_at": activity.occurred_at.isoformat() if activity.occurred_at else "",
        "contact_id": str(activity.contact_id) if activity.contact_id else "",
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


@transaction.atomic
def create_prospect_activity(
    *,
    tenant,
    prospect,
    activity_type,
    note,
    occurred_at=None,
    contact=None,
    actor=None,
    request=None,
):
    locked_prospect = _validate_prospect_tenant(tenant=tenant, prospect=prospect)
    locked_contact = _resolve_contact(tenant=tenant, prospect=locked_prospect, contact=contact)
    if activity_type not in ProspectActivity.ActivityType.values:
        raise ValidationError({"activity_type": "Invalid activity type."})
    when = occurred_at or timezone.now()
    activity = ProspectActivity(
        tenant=tenant,
        prospect=locked_prospect,
        contact=locked_contact,
        activity_type=activity_type,
        note=note,
        occurred_at=when,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    activity.full_clean()
    activity.save()
    record_audit_event(
        action=ACTION_PROSPECT_ACTIVITY_CREATED,
        actor=actor,
        tenant=tenant,
        obj=activity,
        after_data=_activity_snapshot(activity),
        metadata={
            "prospect_id": str(locked_prospect.id),
            "activity_id": str(activity.id),
            "contact_id": str(locked_contact.id) if locked_contact else "",
        },
        request=request,
    )
    return activity


@transaction.atomic
def update_prospect_activity(
    *,
    tenant,
    activity,
    activity_type,
    note,
    occurred_at,
    contact=None,
    actor=None,
    request=None,
):
    locked = ProspectActivity.objects.select_for_update().select_related("prospect", "contact").get(pk=activity.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Activity does not belong to the tenant.")
    before = _activity_snapshot(locked)
    locked_contact = _resolve_contact(tenant=tenant, prospect=locked.prospect, contact=contact)
    if activity_type not in ProspectActivity.ActivityType.values:
        raise ValidationError({"activity_type": "Invalid activity type."})
    locked.activity_type = activity_type
    locked.note = note
    locked.occurred_at = occurred_at or locked.occurred_at
    locked.contact = locked_contact
    locked.full_clean()
    after = _activity_snapshot(locked)
    if before == after:
        return locked, False
    locked.save(update_fields=["activity_type", "note", "occurred_at", "contact", "updated_at"])
    record_audit_event(
        action=ACTION_PROSPECT_ACTIVITY_UPDATED,
        actor=actor,
        tenant=tenant,
        obj=locked,
        before_data=before,
        after_data=after,
        metadata={
            "prospect_id": str(locked.prospect_id),
            "activity_id": str(locked.id),
            "contact_id": str(locked.contact_id) if locked.contact_id else "",
        },
        request=request,
    )
    return locked, True


@transaction.atomic
def delete_prospect_activity(*, tenant, activity, actor=None, request=None):
    locked = ProspectActivity.objects.select_for_update().select_related("prospect").get(pk=activity.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Activity does not belong to the tenant.")
    before = _activity_snapshot(locked)
    prospect_id = str(locked.prospect_id)
    activity_id = str(locked.id)
    locked.delete()
    record_audit_event(
        action=ACTION_PROSPECT_ACTIVITY_DELETED,
        actor=actor,
        tenant=tenant,
        object_type="prospecting.ProspectActivity",
        object_id=activity_id,
        object_repr=before.get("note", "")[:120],
        before_data=before,
        after_data={},
        metadata={"prospect_id": prospect_id, "activity_id": activity_id},
        request=request,
    )
