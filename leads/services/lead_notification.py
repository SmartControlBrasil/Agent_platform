from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from assistant_core.summary import build_lead_notification_body, build_lead_notification_subject
from leads.services.commercial import is_ready_for_commercial_notification

logger = logging.getLogger(__name__)

NOTIFICATION_SENT_KEY = "lead_notification_sent_at"
NOTIFICATION_DRY_RUN_KEY = "lead_notification_dry_run_at"
NOTIFICATION_DRY_RUN_FLAG_KEY = "lead_notification_dry_run"


@dataclass(frozen=True)
class LeadNotificationResult:
    success: bool
    dry_run: bool
    skipped: bool
    message: str


class LeadNotificationService:
    channel = "email"

    def notify(self, lead_draft) -> LeadNotificationResult:
        enabled = bool(getattr(settings, "LIVIA_LEAD_NOTIFICATIONS_ENABLED", False))
        dry_run = bool(getattr(settings, "LIVIA_LEAD_NOTIFICATIONS_DRY_RUN", True))
        recipient = self._recipient_for(lead_draft)

        if not (is_ready_for_commercial_notification(lead_draft) or self._legacy_qualified_lead_ready(lead_draft)):
            message = "Lead not ready for commercial notification; skipping."
            self._log("lead_notification_not_ready", lead_draft, message=message)
            return LeadNotificationResult(success=True, dry_run=dry_run, skipped=True, message=message)

        if self._already_sent(lead_draft):
            message = "Lead notification already sent; skipping duplicate."
            self._log("lead_notification_skipped_duplicate", lead_draft, message=message)
            return LeadNotificationResult(success=True, dry_run=False, skipped=True, message=message)

        if not enabled:
            message = "Lead notifications disabled; skipping."
            self._log("lead_notification_disabled", lead_draft, message=message)
            return LeadNotificationResult(success=True, dry_run=True, skipped=True, message=message)

        if dry_run:
            if self._already_dry_run(lead_draft):
                message = "Lead notification dry-run already recorded; skipping duplicate."
                self._log("lead_notification_dry_run_duplicate", lead_draft, message=message)
                return LeadNotificationResult(success=True, dry_run=True, skipped=True, message=message)
            timestamp = timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M")
            subject = build_lead_notification_subject(lead_draft)
            body = build_lead_notification_body(lead_draft, timestamp=timestamp)
            message = f"Dry-run lead notification prepared for {recipient or 'no-recipient'}."
            self._mark_dry_run(lead_draft)
            self._log(
                "lead_notification_dry_run",
                lead_draft,
                message=f"{message} subject={subject[:80]} body_chars={len(body)}",
            )
            return LeadNotificationResult(success=True, dry_run=True, skipped=False, message=message)

        if not recipient:
            message = "Lead notification recipient is not configured."
            self._log("lead_notification_missing_recipient", lead_draft, message=message)
            return LeadNotificationResult(success=False, dry_run=False, skipped=False, message=message)

        timestamp = timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M")
        subject = build_lead_notification_subject(lead_draft)
        body = build_lead_notification_body(lead_draft, timestamp=timestamp)
        from_email = str(getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip() or recipient
        bcc = self._bcc_list()
        email = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=from_email,
            to=[recipient],
            bcc=bcc,
        )
        try:
            email.send(fail_silently=False)
        except Exception as exc:
            message = f"Lead notification delivery failed: {exc.__class__.__name__}"
            self._log("lead_notification_failed", lead_draft, message=message)
            return LeadNotificationResult(success=False, dry_run=False, skipped=False, message=message)

        self._mark_delivered(lead_draft)
        message = f"Lead notification sent to {recipient}."
        self._log("lead_notification_sent", lead_draft, message=message)
        return LeadNotificationResult(success=True, dry_run=False, skipped=False, message=message)

    def _legacy_qualified_lead_ready(self, lead_draft) -> bool:
        if lead_draft is None:
            return False
        status = str(getattr(lead_draft, "status", "") or "")
        has_contact = bool(str(getattr(lead_draft, "phone", "") or "").strip() or str(getattr(lead_draft, "email", "") or "").strip())
        return (
            status == "qualified"
            and bool(str(getattr(lead_draft, "name", "") or "").strip())
            and bool(str(getattr(lead_draft, "need_summary", "") or "").strip())
            and has_contact
        )

    def _recipient_for(self, lead_draft) -> str:
        tenant = getattr(lead_draft, "tenant", None)
        profile = getattr(tenant, "assistant_profile", None) if tenant is not None else None
        profile_email = str(getattr(profile, "notification_email", "") or "").strip()
        if profile_email:
            return profile_email
        return str(getattr(settings, "LIVIA_LEAD_NOTIFICATION_EMAIL", "") or "").strip()

    def _bcc_list(self) -> list[str]:
        raw = str(getattr(settings, "LIVIA_LEAD_NOTIFICATION_BCC", "") or "").strip()
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]

    def _already_sent(self, lead_draft) -> bool:
        data = getattr(lead_draft, "qualification_data", None) or {}
        return bool(isinstance(data, dict) and data.get(NOTIFICATION_SENT_KEY))

    def _already_dry_run(self, lead_draft) -> bool:
        data = getattr(lead_draft, "qualification_data", None) or {}
        return bool(isinstance(data, dict) and data.get(NOTIFICATION_DRY_RUN_KEY))

    def _mark_delivered(self, lead_draft) -> None:
        if lead_draft is None or not hasattr(lead_draft, "qualification_data"):
            return
        data = dict(getattr(lead_draft, "qualification_data", None) or {})
        data[NOTIFICATION_SENT_KEY] = timezone.now().isoformat()
        lead_draft.qualification_data = data
        lead_draft.save(update_fields=["qualification_data", "updated_at"])

    def _mark_dry_run(self, lead_draft) -> None:
        if lead_draft is None or not hasattr(lead_draft, "qualification_data"):
            return
        data = dict(getattr(lead_draft, "qualification_data", None) or {})
        data[NOTIFICATION_DRY_RUN_KEY] = timezone.now().isoformat()
        data[NOTIFICATION_DRY_RUN_FLAG_KEY] = True
        lead_draft.qualification_data = data
        lead_draft.save(update_fields=["qualification_data", "updated_at"])

    def _log(self, event: str, lead_draft, *, message: str) -> None:
        logger.info(
            "event=%s lead_draft_id=%s tenant_slug=%s status=%s message=%s",
            event,
            getattr(lead_draft, "id", ""),
            getattr(getattr(lead_draft, "tenant", None), "slug", ""),
            getattr(lead_draft, "status", ""),
            message,
        )
