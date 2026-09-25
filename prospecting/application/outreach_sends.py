from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record_audit_event
from prospecting.application.ports.email_sender import EmailSenderPort, get_default_email_sender
from prospecting.models import Prospect, ProspectActivity, ProspectOutreachDraft, ProspectOutreachSend

ACTION_OUTREACH_SEND_REQUESTED = "prospecting.outreach_send.requested"
ACTION_OUTREACH_SEND_SENT = "prospecting.outreach_send.sent"
ACTION_OUTREACH_SEND_FAILED = "prospecting.outreach_send.failed"
ACTION_OUTREACH_SEND_RETRY_REQUESTED = "prospecting.outreach_send.retry_requested"


def _draft_idempotency_key(draft_id) -> str:
    return f"prospecting:outreach_send:draft:{draft_id}"


def _send_snapshot(send):
    return {
        "status": send.status,
        "destination": send.destination,
        "subject_snapshot": send.subject_snapshot,
        "attempt_count": send.attempt_count,
        "provider_message_id": send.provider_message_id or "",
    }


def _validate_draft_for_email_send(*, tenant, draft):
    locked_draft = ProspectOutreachDraft.objects.select_for_update().select_related("prospect", "contact").get(pk=draft.pk)
    if locked_draft.tenant_id != tenant.id:
        raise ValidationError("Outreach draft does not belong to the tenant.")
    if locked_draft.channel != ProspectOutreachDraft.Channel.EMAIL:
        raise ValidationError("Only EMAIL outreach drafts can be sent.")
    if locked_draft.status != ProspectOutreachDraft.Status.READY:
        raise ValidationError("Outreach draft must be READY before sending email.")
    if locked_draft.status == ProspectOutreachDraft.Status.ARCHIVED:
        raise ValidationError("Archived outreach drafts cannot be sent.")
    locked_prospect = Prospect.objects.select_for_update().get(pk=locked_draft.prospect_id)
    if locked_prospect.tenant_id != tenant.id:
        raise ValidationError("Prospect does not belong to the tenant.")
    if locked_prospect.qualification_status != Prospect.QualificationStatus.QUALIFIED:
        raise ValidationError("Email send is allowed only while the prospect remains QUALIFIED.")
    destination = (locked_draft.destination_email or "").strip()
    subject = (locked_draft.subject or "").strip()
    body = (locked_draft.body or "").strip()
    if not destination:
        raise ValidationError("Outreach draft has no email destination snapshot.")
    if not subject:
        raise ValidationError({"subject": "Email subject is required."})
    if not body:
        raise ValidationError({"body": "Email body is required."})
    return locked_draft, locked_prospect, destination, subject, body


def _create_activity_for_sent_email(*, tenant, send, actor):
    if send.prospect_activity_id:
        return send.prospect_activity
    contact_name = send.contact.name or send.contact.email or send.contact.phone or "—"
    note = f"Destino: {send.destination}\nAssunto: {send.subject_snapshot}"
    activity = ProspectActivity(
        tenant=tenant,
        prospect=send.prospect,
        contact=send.contact,
        activity_type=ProspectActivity.ActivityType.EMAIL_SENT,
        note=note,
        occurred_at=send.sent_at or timezone.now(),
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    activity.full_clean()
    activity.save()
    send.prospect_activity = activity
    send.save(update_fields=["prospect_activity", "updated_at"])
    return activity


@transaction.atomic
def _lock_or_create_send(*, tenant, draft, actor, destination, subject, body):
    idempotency_key = _draft_idempotency_key(draft.id)
    try:
        send = (
            ProspectOutreachSend.objects.select_for_update()
            .select_related("draft", "prospect", "contact")
            .get(draft_id=draft.id)
        )
    except ProspectOutreachSend.DoesNotExist:
        send = ProspectOutreachSend(
            tenant=tenant,
            draft=draft,
            prospect=draft.prospect,
            contact=draft.contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            destination=destination,
            subject_snapshot=subject,
            body_snapshot=body,
            status=ProspectOutreachSend.Status.PENDING,
            idempotency_key=idempotency_key,
            requested_by=actor if getattr(actor, "is_authenticated", False) else None,
        )
        try:
            send.save()
        except IntegrityError:
            send = ProspectOutreachSend.objects.select_for_update().get(draft_id=draft.id)
    else:
        send = ProspectOutreachSend.objects.select_for_update().select_related("draft", "prospect", "contact").get(pk=send.pk)
    return send


def _deliver_locked_send(*, tenant, send, actor, request, email_sender, retry=False):
    if send.status == ProspectOutreachSend.Status.SENT:
        return send, False
    if send.status == ProspectOutreachSend.Status.SENDING:
        raise ValidationError("An email send is already in progress for this draft.")
    if send.status == ProspectOutreachSend.Status.CANCELLED:
        raise ValidationError("This outreach send was cancelled.")
    if retry and send.status != ProspectOutreachSend.Status.FAILED:
        raise ValidationError("Only failed sends can be retried.")

    before_status = send.status
    send.status = ProspectOutreachSend.Status.SENDING
    send.attempt_count += 1
    send.error_code = ""
    send.error_message = ""
    send.save(update_fields=["status", "attempt_count", "error_code", "error_message", "updated_at"])

    action = ACTION_OUTREACH_SEND_RETRY_REQUESTED if retry else ACTION_OUTREACH_SEND_REQUESTED
    record_audit_event(
        action=action,
        actor=actor,
        tenant=tenant,
        obj=send,
        before_data={"status": before_status, "attempt_count": send.attempt_count - 1},
        after_data={"status": send.status, "attempt_count": send.attempt_count},
        metadata={
            "prospect_id": str(send.prospect_id),
            "draft_id": str(send.draft_id),
            "send_id": str(send.id),
            "destination": send.destination,
        },
        request=request,
    )

    result = email_sender.send_plain_email(
        to=send.destination,
        subject=send.subject_snapshot,
        body=send.body_snapshot,
        idempotency_key=send.idempotency_key,
    )

    if result.success:
        send.status = ProspectOutreachSend.Status.SENT
        send.sent_at = timezone.now()
        send.failed_at = None
        send.provider_message_id = (result.provider_message_id or "")[:220]
        send.save(
            update_fields=[
                "status",
                "sent_at",
                "failed_at",
                "provider_message_id",
                "updated_at",
            ]
        )
        _create_activity_for_sent_email(tenant=tenant, send=send, actor=actor)
        record_audit_event(
            action=ACTION_OUTREACH_SEND_SENT,
            actor=actor,
            tenant=tenant,
            obj=send,
            after_data=_send_snapshot(send),
            metadata={
                "prospect_id": str(send.prospect_id),
                "draft_id": str(send.draft_id),
                "send_id": str(send.id),
                "activity_id": str(send.prospect_activity_id or ""),
            },
            request=request,
        )
        return send, True

    send.status = ProspectOutreachSend.Status.FAILED
    send.failed_at = timezone.now()
    send.error_code = (result.error_code or "delivery_failed")[:120]
    send.error_message = (result.error_message or "Email delivery failed.")[:500]
    send.save(update_fields=["status", "failed_at", "error_code", "error_message", "updated_at"])
    record_audit_event(
        action=ACTION_OUTREACH_SEND_FAILED,
        actor=actor,
        tenant=tenant,
        obj=send,
        after_data=_send_snapshot(send),
        metadata={
            "prospect_id": str(send.prospect_id),
            "draft_id": str(send.draft_id),
            "send_id": str(send.id),
            "error_code": send.error_code,
        },
        request=request,
    )
    return send, True


@transaction.atomic
def send_outreach_draft_email(
    *,
    tenant,
    draft,
    actor=None,
    request=None,
    email_sender: EmailSenderPort | None = None,
):
    sender = email_sender or get_default_email_sender()
    locked_draft, _prospect, destination, subject, body = _validate_draft_for_email_send(tenant=tenant, draft=draft)
    send = _lock_or_create_send(
        tenant=tenant,
        draft=locked_draft,
        actor=actor,
        destination=destination,
        subject=subject,
        body=body,
    )
    if send.status == ProspectOutreachSend.Status.SENT:
        return send, False
    if send.status == ProspectOutreachSend.Status.FAILED:
        raise ValidationError("This draft already has a failed send. Use retry instead of sending again.")
    return _deliver_locked_send(
        tenant=tenant,
        send=send,
        actor=actor,
        request=request,
        email_sender=sender,
        retry=False,
    )


@transaction.atomic
def retry_outreach_send(
    *,
    tenant,
    send,
    actor=None,
    request=None,
    email_sender: EmailSenderPort | None = None,
):
    sender = email_sender or get_default_email_sender()
    locked = ProspectOutreachSend.objects.select_for_update().select_related("draft", "prospect", "contact").get(pk=send.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Outreach send does not belong to the tenant.")
    _validate_draft_for_email_send(tenant=tenant, draft=locked.draft)
    return _deliver_locked_send(
        tenant=tenant,
        send=locked,
        actor=actor,
        request=request,
        email_sender=sender,
        retry=True,
    )
