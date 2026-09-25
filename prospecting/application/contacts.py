from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from audit.services import record_audit_event
from prospecting.models import Prospect, ProspectContact, ProspectEnrichment

ACTION_PROSPECT_CONTACT_CREATED = "prospecting.contact.created"
ACTION_PROSPECT_CONTACT_UPDATED = "prospecting.contact.updated"
ACTION_PROSPECT_CONTACT_DELETED = "prospecting.contact.deleted"


def _contact_snapshot(contact):
    return {
        "name": contact.name or "",
        "role_title": contact.role_title or "",
        "email": contact.email or "",
        "phone": contact.phone or "",
        "note": contact.note or "",
        "source_type": contact.source_type,
        "source_reference": contact.source_reference or "",
        "source_url": contact.source_url or "",
    }


def _validate_prospect_tenant(*, tenant, prospect):
    locked = Prospect.objects.select_for_update().get(pk=prospect.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Prospect does not belong to the tenant.")
    return locked


def _resolve_source_enrichment(*, tenant, prospect, source_enrichment):
    if source_enrichment is None:
        return None
    enrichment = ProspectEnrichment.objects.select_related("tenant", "prospect").get(pk=source_enrichment.pk)
    if enrichment.tenant_id != tenant.id:
        raise ValidationError("Enrichment source does not belong to the tenant.")
    if enrichment.prospect_id != prospect.id:
        raise ValidationError("Enrichment source must belong to the same prospect.")
    return enrichment


def _build_contact(**kwargs):
    contact = ProspectContact(**kwargs)
    contact.full_clean()
    return contact


def _save_contact(contact):
    try:
        contact.save()
    except IntegrityError as exc:
        raise ValidationError("A contact with the same email or phone already exists for this prospect.") from exc
    return contact


@transaction.atomic
def create_prospect_contact(
    *,
    tenant,
    prospect,
    name="",
    role_title="",
    email="",
    phone="",
    note="",
    source_type=ProspectContact.SourceType.MANUAL,
    source_reference="",
    source_url="",
    source_enrichment=None,
    actor=None,
    request=None,
):
    locked_prospect = _validate_prospect_tenant(tenant=tenant, prospect=prospect)
    enrichment = _resolve_source_enrichment(tenant=tenant, prospect=locked_prospect, source_enrichment=source_enrichment)
    contact = _build_contact(
        tenant=tenant,
        prospect=locked_prospect,
        name=name,
        role_title=role_title,
        email=email,
        phone=phone,
        note=note,
        source_type=source_type,
        source_reference=source_reference,
        source_url=source_url,
        source_enrichment=enrichment,
    )
    _save_contact(contact)
    record_audit_event(
        action=ACTION_PROSPECT_CONTACT_CREATED,
        actor=actor,
        tenant=tenant,
        obj=contact,
        after_data=_contact_snapshot(contact),
        metadata={"prospect_id": str(locked_prospect.id), "contact_id": str(contact.id)},
        request=request,
    )
    return contact


@transaction.atomic
def update_prospect_contact(
    *,
    tenant,
    contact,
    name="",
    role_title="",
    email="",
    phone="",
    note="",
    source_type=None,
    source_reference="",
    source_url="",
    actor=None,
    request=None,
):
    locked = ProspectContact.objects.select_for_update().select_related("prospect").get(pk=contact.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Contact does not belong to the tenant.")
    before = _contact_snapshot(locked)
    locked.name = name
    locked.role_title = role_title
    locked.email = email
    locked.phone = phone
    locked.note = note
    if source_type is not None:
        locked.source_type = source_type
    locked.source_reference = source_reference
    locked.source_url = source_url
    locked.full_clean()
    after = _contact_snapshot(locked)
    if before == after:
        return locked, False
    try:
        locked.save(
            update_fields=[
                "name",
                "role_title",
                "email",
                "phone",
                "normalized_email",
                "normalized_phone",
                "note",
                "source_type",
                "source_reference",
                "source_url",
                "updated_at",
            ]
        )
    except IntegrityError as exc:
        raise ValidationError("A contact with the same email or phone already exists for this prospect.") from exc
    record_audit_event(
        action=ACTION_PROSPECT_CONTACT_UPDATED,
        actor=actor,
        tenant=tenant,
        obj=locked,
        before_data=before,
        after_data=after,
        metadata={"prospect_id": str(locked.prospect_id), "contact_id": str(locked.id)},
        request=request,
    )
    return locked, True


@transaction.atomic
def delete_prospect_contact(*, tenant, contact, actor=None, request=None):
    locked = ProspectContact.objects.select_for_update().select_related("prospect").get(pk=contact.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Contact does not belong to the tenant.")
    before = _contact_snapshot(locked)
    prospect_id = str(locked.prospect_id)
    contact_id = str(locked.id)
    locked.delete()
    record_audit_event(
        action=ACTION_PROSPECT_CONTACT_DELETED,
        actor=actor,
        tenant=tenant,
        object_type="prospecting.ProspectContact",
        object_id=contact_id,
        object_repr=before.get("name") or before.get("email") or contact_id,
        before_data=before,
        after_data={},
        metadata={"prospect_id": prospect_id, "contact_id": contact_id},
        request=request,
    )
