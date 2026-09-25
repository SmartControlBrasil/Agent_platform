from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit_event
from prospecting.models import Prospect

ACTION_PROSPECT_QUALIFICATION_UPDATED = "prospecting.prospect.qualification_updated"

QUALIFICATION_AUDIT_FIELDS = ("qualification_status", "priority", "qualification_note")


def _qualification_snapshot(prospect):
    return {
        "qualification_status": prospect.qualification_status,
        "priority": prospect.priority,
        "qualification_note": prospect.qualification_note or "",
    }


@transaction.atomic
def qualify_prospect(
    *,
    tenant,
    prospect,
    qualification_status,
    priority,
    qualification_note="",
    actor=None,
    request=None,
):
    locked = Prospect.objects.select_for_update().get(pk=prospect.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Prospect does not belong to the tenant.")

    if qualification_status not in Prospect.QualificationStatus.values:
        raise ValidationError({"qualification_status": "Invalid qualification status."})
    if priority not in Prospect.Priority.values:
        raise ValidationError({"priority": "Invalid priority."})

    note = (qualification_note or "").strip()
    if len(note) > 2000:
        raise ValidationError({"qualification_note": "Qualification note must be at most 2000 characters."})

    before = _qualification_snapshot(locked)
    after = {
        "qualification_status": qualification_status,
        "priority": priority,
        "qualification_note": note,
    }
    if before == after:
        return locked, False

    locked.qualification_status = qualification_status
    locked.priority = priority
    locked.qualification_note = note
    locked.qualified_at = timezone.now()
    locked.qualified_by = actor if getattr(actor, "is_authenticated", False) else None
    locked.save(
        update_fields=[
            "qualification_status",
            "priority",
            "qualification_note",
            "qualified_at",
            "qualified_by",
            "updated_at",
        ]
    )
    record_audit_event(
        action=ACTION_PROSPECT_QUALIFICATION_UPDATED,
        actor=actor,
        tenant=locked.tenant,
        obj=locked,
        before_data=before,
        after_data=after,
        metadata={
            "prospect_id": str(locked.id),
            "qualified_by_id": getattr(actor, "pk", None),
        },
        request=request,
    )
    return locked, True
