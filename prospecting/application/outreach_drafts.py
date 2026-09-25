from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit_event
from prospecting.models import Prospect, ProspectContact, ProspectOutreachDraft

ACTION_OUTREACH_DRAFT_CREATED = "prospecting.outreach_draft.created"
ACTION_OUTREACH_DRAFT_UPDATED = "prospecting.outreach_draft.updated"
ACTION_OUTREACH_DRAFT_READY = "prospecting.outreach_draft.ready"
ACTION_OUTREACH_DRAFT_ARCHIVED = "prospecting.outreach_draft.archived"

ALLOWED_STATUS_TRANSITIONS = {
    ProspectOutreachDraft.Status.DRAFT: {
        ProspectOutreachDraft.Status.READY,
        ProspectOutreachDraft.Status.ARCHIVED,
    },
    ProspectOutreachDraft.Status.READY: {
        ProspectOutreachDraft.Status.DRAFT,
        ProspectOutreachDraft.Status.ARCHIVED,
    },
    ProspectOutreachDraft.Status.ARCHIVED: {
        ProspectOutreachDraft.Status.DRAFT,
    },
}


def _draft_snapshot(draft):
    return {
        "channel": draft.channel,
        "subject": draft.subject or "",
        "body": draft.body,
        "status": draft.status,
        "contact_id": str(draft.contact_id),
        "destination_email": draft.destination_email or "",
        "destination_phone": draft.destination_phone or "",
    }


def _validate_prospect_for_outreach(*, tenant, prospect):
    locked = Prospect.objects.select_for_update().get(pk=prospect.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Prospect does not belong to the tenant.")
    if locked.qualification_status != Prospect.QualificationStatus.QUALIFIED:
        raise ValidationError("Outreach drafts are allowed only for qualified prospects.")
    return locked


def _resolve_contact(*, tenant, prospect, contact):
    if contact is None:
        raise ValidationError({"contact": "Contact is required for outreach drafts."})
    locked = ProspectContact.objects.select_related("prospect").get(pk=contact.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Contact does not belong to the tenant.")
    if locked.prospect_id != prospect.id:
        raise ValidationError("Contact must belong to the same prospect.")
    return locked


def _apply_destination_snapshots(*, draft, contact, channel):
    draft.destination_email = ""
    draft.destination_phone = ""
    if channel == ProspectOutreachDraft.Channel.EMAIL:
        if not contact.email:
            raise ValidationError({"contact": "Selected contact has no email for an email outreach draft."})
        if not draft.subject:
            raise ValidationError({"subject": "Subject is required for email outreach drafts."})
        draft.destination_email = contact.email
    elif channel in {ProspectOutreachDraft.Channel.PHONE, ProspectOutreachDraft.Channel.WHATSAPP}:
        if not contact.phone:
            raise ValidationError({"contact": "Selected contact has no phone for this outreach channel."})
        draft.destination_phone = contact.phone
    elif channel == ProspectOutreachDraft.Channel.OTHER:
        if not (contact.name or contact.email or contact.phone):
            raise ValidationError({"contact": "Selected contact has no identifiable destination."})
        draft.destination_email = contact.email or ""
        draft.destination_phone = contact.phone or ""


def _transition_status(*, draft, new_status):
    if new_status == draft.status:
        return
    allowed = ALLOWED_STATUS_TRANSITIONS.get(draft.status, set())
    if new_status not in allowed:
        raise ValidationError(f"Invalid outreach draft status transition: {draft.status} -> {new_status}.")
    draft.status = new_status
    if new_status == ProspectOutreachDraft.Status.READY:
        draft.prepared_at = timezone.now()
    elif new_status == ProspectOutreachDraft.Status.DRAFT:
        draft.prepared_at = None


def _record_status_audit(*, action, draft, actor, before_status, request=None):
    record_audit_event(
        action=action,
        actor=actor,
        tenant=draft.tenant,
        obj=draft,
        before_data={"status": before_status},
        after_data={"status": draft.status},
        metadata={"prospect_id": str(draft.prospect_id), "draft_id": str(draft.id)},
        request=request,
    )


@transaction.atomic
def create_outreach_draft(
    *,
    tenant,
    prospect,
    contact,
    channel,
    subject="",
    body,
    status=ProspectOutreachDraft.Status.DRAFT,
    actor=None,
    request=None,
):
    locked_prospect = _validate_prospect_for_outreach(tenant=tenant, prospect=prospect)
    locked_contact = _resolve_contact(tenant=tenant, prospect=locked_prospect, contact=contact)
    channel = str(channel or "").upper()
    status = str(status or ProspectOutreachDraft.Status.DRAFT).upper()
    if status not in ProspectOutreachDraft.Status.values:
        raise ValidationError({"status": "Invalid outreach draft status."})
    draft = ProspectOutreachDraft(
        tenant=tenant,
        prospect=locked_prospect,
        contact=locked_contact,
        channel=channel,
        subject=subject or "",
        body=body,
        status=ProspectOutreachDraft.Status.DRAFT,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    _apply_destination_snapshots(draft=draft, contact=locked_contact, channel=channel)
    draft.full_clean()
    if status != ProspectOutreachDraft.Status.DRAFT:
        _transition_status(draft=draft, new_status=status)
    draft.save()
    record_audit_event(
        action=ACTION_OUTREACH_DRAFT_CREATED,
        actor=actor,
        tenant=tenant,
        obj=draft,
        after_data=_draft_snapshot(draft),
        metadata={"prospect_id": str(locked_prospect.id), "draft_id": str(draft.id)},
        request=request,
    )
    if status == ProspectOutreachDraft.Status.READY:
        _record_status_audit(
            action=ACTION_OUTREACH_DRAFT_READY,
            draft=draft,
            actor=actor,
            before_status=ProspectOutreachDraft.Status.DRAFT,
            request=request,
        )
    return draft


@transaction.atomic
def update_outreach_draft(
    *,
    tenant,
    draft,
    contact,
    channel,
    subject="",
    body,
    actor=None,
    request=None,
):
    locked = ProspectOutreachDraft.objects.select_for_update().select_related("prospect", "contact").get(pk=draft.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Outreach draft does not belong to the tenant.")
    if locked.status == ProspectOutreachDraft.Status.ARCHIVED:
        raise ValidationError("Archived outreach drafts cannot be edited. Restore to draft first.")
    _validate_prospect_for_outreach(tenant=tenant, prospect=locked.prospect)
    locked_contact = _resolve_contact(tenant=tenant, prospect=locked.prospect, contact=contact)
    before = _draft_snapshot(locked)
    locked.contact = locked_contact
    locked.channel = str(channel or "").upper()
    locked.subject = subject or ""
    locked.body = body
    _apply_destination_snapshots(draft=locked, contact=locked_contact, channel=locked.channel)
    locked.full_clean()
    after = _draft_snapshot(locked)
    if before == after:
        return locked, False
    locked.save(
        update_fields=[
            "contact",
            "channel",
            "subject",
            "body",
            "destination_email",
            "destination_phone",
            "updated_at",
        ]
    )
    record_audit_event(
        action=ACTION_OUTREACH_DRAFT_UPDATED,
        actor=actor,
        tenant=tenant,
        obj=locked,
        before_data=before,
        after_data=after,
        metadata={"prospect_id": str(locked.prospect_id), "draft_id": str(locked.id)},
        request=request,
    )
    return locked, True


@transaction.atomic
def mark_outreach_draft_ready(*, tenant, draft, actor=None, request=None):
    locked = ProspectOutreachDraft.objects.select_for_update().select_related("prospect").get(pk=draft.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Outreach draft does not belong to the tenant.")
    before_status = locked.status
    _transition_status(draft=locked, new_status=ProspectOutreachDraft.Status.READY)
    locked.save(update_fields=["status", "prepared_at", "updated_at"])
    _record_status_audit(
        action=ACTION_OUTREACH_DRAFT_READY,
        draft=locked,
        actor=actor,
        before_status=before_status,
        request=request,
    )
    return locked


@transaction.atomic
def revert_outreach_draft_to_draft(*, tenant, draft, actor=None, request=None):
    locked = ProspectOutreachDraft.objects.select_for_update().select_related("prospect").get(pk=draft.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Outreach draft does not belong to the tenant.")
    before_status = locked.status
    _transition_status(draft=locked, new_status=ProspectOutreachDraft.Status.DRAFT)
    locked.save(update_fields=["status", "prepared_at", "updated_at"])
    record_audit_event(
        action=ACTION_OUTREACH_DRAFT_UPDATED,
        actor=actor,
        tenant=tenant,
        obj=locked,
        before_data={"status": before_status},
        after_data={"status": locked.status},
        metadata={"prospect_id": str(locked.prospect_id), "draft_id": str(locked.id), "reverted_to_draft": True},
        request=request,
    )
    return locked


@transaction.atomic
def restore_archived_outreach_draft(*, tenant, draft, actor=None, request=None):
    locked = ProspectOutreachDraft.objects.select_for_update().select_related("prospect").get(pk=draft.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Outreach draft does not belong to the tenant.")
    before_status = locked.status
    _transition_status(draft=locked, new_status=ProspectOutreachDraft.Status.DRAFT)
    locked.save(update_fields=["status", "prepared_at", "updated_at"])
    record_audit_event(
        action=ACTION_OUTREACH_DRAFT_UPDATED,
        actor=actor,
        tenant=tenant,
        obj=locked,
        before_data={"status": before_status},
        after_data={"status": locked.status},
        metadata={"prospect_id": str(locked.prospect_id), "draft_id": str(locked.id), "restored_from_archive": True},
        request=request,
    )
    return locked


@transaction.atomic
def archive_outreach_draft(*, tenant, draft, actor=None, request=None):
    locked = ProspectOutreachDraft.objects.select_for_update().select_related("prospect").get(pk=draft.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Outreach draft does not belong to the tenant.")
    before_status = locked.status
    _transition_status(draft=locked, new_status=ProspectOutreachDraft.Status.ARCHIVED)
    locked.save(update_fields=["status", "updated_at"])
    _record_status_audit(
        action=ACTION_OUTREACH_DRAFT_ARCHIVED,
        draft=locked,
        actor=actor,
        before_status=before_status,
        request=request,
    )
    return locked
