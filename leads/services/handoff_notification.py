from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from assistant_core.summary import (
    build_commercial_notification_html,
    build_handoff_notification_body,
    build_handoff_notification_subject,
)
from leads.services.commercial_notification import (
    cycle_already_dry_run,
    cycle_already_notified,
    has_usable_cycle_contact,
    mark_cycle_notified,
    related_lead,
)

logger = logging.getLogger(__name__)

NOTIFICATION_SENT_KEY = "handoff_notification_sent_at"


@dataclass(frozen=True)
class HandoffNotificationResult:
    success: bool
    dry_run: bool
    channel: str
    message: str
    skipped: bool = False


class HandoffNotificationService:
    channel = "email"

    def notify(self, handoff) -> HandoffNotificationResult:
        enabled = bool(getattr(settings, "LIVIA_HANDOFF_NOTIFICATIONS_ENABLED", False))
        dry_run = bool(getattr(settings, "LIVIA_HANDOFF_NOTIFICATIONS_DRY_RUN", True))
        recipient = self._recipient_for(handoff)

        lead = related_lead(handoff=handoff)
        if cycle_already_notified(lead=lead, handoff=handoff) or self._already_sent(handoff):
            message = "Handoff notification already sent; skipping duplicate."
            self._log("handoff_notification_skipped_duplicate", handoff, message=message)
            return HandoffNotificationResult(
                success=True, dry_run=dry_run, channel=self.channel, message=message, skipped=True
            )

        if not has_usable_cycle_contact(lead=lead, handoff=handoff):
            message = "Handoff has no usable contact; skipping commercial email."
            self._log("handoff_notification_missing_contact", handoff, message=message)
            return HandoffNotificationResult(
                success=True, dry_run=dry_run, channel=self.channel, message=message, skipped=True
            )

        if not enabled:
            message = "Handoff notifications disabled; skipping."
            self._log("handoff_notification_disabled", handoff, message=message)
            return HandoffNotificationResult(
                success=True, dry_run=True, channel=self.channel, message=message, skipped=True
            )

        if dry_run:
            if cycle_already_dry_run(lead=lead, handoff=handoff):
                message = "Handoff notification dry-run already recorded; skipping duplicate."
                self._log("handoff_notification_dry_run_duplicate", handoff, message=message)
                return HandoffNotificationResult(
                    success=True, dry_run=True, channel=self.channel, message=message, skipped=True
                )
            message = f"Dry-run handoff notification prepared for {recipient or 'no-recipient'}."
            self._mark_sent(handoff, dry_run=True, lead=lead)
            self._log("handoff_notification_dry_run", handoff, message=message)
            return HandoffNotificationResult(
                success=True, dry_run=True, channel=self.channel, message=message, skipped=False
            )

        if not recipient:
            message = "Handoff notification recipient is not configured."
            self._log("handoff_notification_missing_recipient", handoff, message=message)
            return HandoffNotificationResult(
                success=False, dry_run=False, channel=self.channel, message=message, skipped=False
            )

        subject = build_handoff_notification_subject(handoff, lead_draft=lead)
        timestamp = timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M")
        body = build_handoff_notification_body(handoff, timestamp=timestamp)
        from_email = str(getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip() or recipient
        bcc = self._bcc_list()
        email = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email,
            to=[recipient],
            bcc=bcc,
        )
        email.attach_alternative(build_commercial_notification_html(body), "text/html")
        try:
            email.send(fail_silently=False)
        except Exception as exc:
            message = f"Handoff notification delivery failed: {exc.__class__.__name__}"
            self._log("handoff_notification_failed", handoff, message=message)
            return HandoffNotificationResult(
                success=False, dry_run=False, channel=self.channel, message=message, skipped=False
            )
        self._mark_sent(handoff, dry_run=False, lead=lead)
        try:
            from conversations.models import HandoffRequest

            if getattr(handoff, "status", "") == HandoffRequest.Status.PENDING:
                handoff.status = HandoffRequest.Status.SENT
                handoff.save(update_fields=["status", "updated_at"])
        except Exception:
            pass
        message = f"Handoff notification sent to {recipient}."
        self._log("handoff_notification_sent", handoff, message=message)
        return HandoffNotificationResult(
            success=True, dry_run=False, channel=self.channel, message=message, skipped=False
        )

    def _recipient_for(self, handoff) -> str:
        tenant = getattr(handoff, "tenant", None)
        profile = getattr(tenant, "assistant_profile", None) if tenant is not None else None
        profile_email = str(getattr(profile, "notification_email", "") or "").strip()
        if profile_email:
            return profile_email
        return str(getattr(settings, "LIVIA_HANDOFF_NOTIFICATION_EMAIL", "") or "").strip()

    def _bcc_list(self) -> list[str]:
        raw = str(getattr(settings, "LIVIA_LEAD_NOTIFICATION_BCC", "") or "").strip()
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]

    def _already_sent(self, handoff) -> bool:
        data = getattr(handoff, "metadata", None) or {}
        return bool(isinstance(data, dict) and data.get(NOTIFICATION_SENT_KEY))

    def _mark_sent(self, handoff, *, dry_run: bool, lead=None) -> None:
        mark_cycle_notified(lead=lead, handoff=handoff, dry_run=dry_run)

    def _log(self, event: str, handoff, *, message: str) -> None:
        logger.info(
            "event=%s handoff_id=%s tenant_slug=%s status=%s priority=%s dry_run_message=%s",
            event,
            getattr(handoff, "id", ""),
            getattr(getattr(handoff, "tenant", None), "slug", ""),
            getattr(handoff, "status", ""),
            getattr(handoff, "priority", ""),
            message,
        )
