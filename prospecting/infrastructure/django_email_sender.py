from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

from prospecting.application.ports.email_sender import EmailSendResult

logger = logging.getLogger(__name__)


class DjangoEmailSenderPort:
    provider = "DJANGO_EMAIL"

    def send_plain_email(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        idempotency_key: str,
    ) -> EmailSendResult:
        enabled = bool(getattr(settings, "PROSPECTING_OUTREACH_EMAIL_ENABLED", True))
        dry_run = bool(getattr(settings, "PROSPECTING_OUTREACH_EMAIL_DRY_RUN", False))
        destination = (to or "").strip()
        if not enabled:
            return EmailSendResult(
                success=False,
                error_code="disabled",
                error_message="Prospect outreach email sending is disabled in this environment.",
            )
        if not destination:
            return EmailSendResult(
                success=False,
                error_code="missing_destination",
                error_message="Email destination is required.",
            )
        if dry_run:
            logger.info(
                "prospecting_outreach_email_dry_run idempotency_key=%s to=%s subject_chars=%s body_chars=%s",
                idempotency_key,
                destination,
                len(subject or ""),
                len(body or ""),
            )
            return EmailSendResult(success=True, provider_message_id=f"dry-run:{idempotency_key}")

        from_email = str(getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip()
        if not from_email:
            return EmailSendResult(
                success=False,
                error_code="missing_from_email",
                error_message="DEFAULT_FROM_EMAIL is not configured.",
            )
        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=from_email,
                recipient_list=[destination],
                fail_silently=False,
            )
        except Exception as exc:
            return EmailSendResult(
                success=False,
                error_code=exc.__class__.__name__,
                error_message=str(exc)[:500],
            )
        return EmailSendResult(success=True, provider_message_id=idempotency_key)
