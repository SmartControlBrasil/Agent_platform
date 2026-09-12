"""Fase 4.1 — semântica de dispatch de notificação comercial."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from assistant_core.consultative_policy import mark_collection_active
from assistant_core.continuity_policy import STATUS_KEY
from conversations.models import Conversation
from integrations.models import OutboxEvent
from integrations.outbox.handlers import LeadQualifiedHandler
from integrations.outbox.payloads import SCHEMA_VERSION
from leads.models import LeadDraft
from leads.services.lead_notification import (
    NOTIFICATION_DRY_RUN_KEY,
    NOTIFICATION_SENT_KEY,
    LeadNotificationService,
)
from tenants.models import AssistantProfile, Tenant

B2B_NEED = "Preciso automatizar o controle de temperatura de três câmaras na empresa."


def _ready_lead(tenant, *, session_id=None):
    conversation = Conversation.objects.create(
        tenant=tenant,
        session_id=session_id or str(uuid.uuid4()),
        source_page="https://example.com/contato",
    )
    lead = LeadDraft.objects.create(
        tenant=tenant,
        conversation=conversation,
        name="Carlos Silva",
        phone="11999998888",
        email="carlos@example.com",
        need_summary=B2B_NEED,
        status=LeadDraft.Status.QUALIFIED,
        qualification_status=LeadDraft.QualificationStatus.QUALIFIED,
        qualification_data={STATUS_KEY: "accepted"},
    )
    mark_collection_active(lead, reason="continuity_offer_accepted")
    return lead


class LeadNotificationDispatchSemanticsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            notification_email="comercial@smartcontrolbrasil.com.br",
        )
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant")
        AssistantProfile.objects.create(
            tenant=self.other_tenant,
            name="Lívia",
            notification_email="contato@outro-tenant.example",
        )

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=False,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_a_notifications_disabled_does_not_send_or_mark_sent(self):
        lead = _ready_lead(self.tenant)
        with patch("leads.services.lead_notification.EmailMultiAlternatives.send") as send_mock:
            result = LeadNotificationService().notify(lead)
        lead.refresh_from_db()
        self.assertTrue(result.skipped)
        self.assertTrue(result.dry_run)
        self.assertFalse((lead.qualification_data or {}).get(NOTIFICATION_SENT_KEY))
        self.assertFalse((lead.qualification_data or {}).get(NOTIFICATION_DRY_RUN_KEY))
        send_mock.assert_not_called()

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_b_dry_run_does_not_send_or_mark_sent(self):
        lead = _ready_lead(self.tenant)
        with patch("leads.services.lead_notification.EmailMultiAlternatives.send") as send_mock:
            result = LeadNotificationService().notify(lead)
        lead.refresh_from_db()
        self.assertTrue(result.success)
        self.assertTrue(result.dry_run)
        self.assertFalse(result.skipped)
        self.assertFalse((lead.qualification_data or {}).get(NOTIFICATION_SENT_KEY))
        self.assertTrue((lead.qualification_data or {}).get(NOTIFICATION_DRY_RUN_KEY))
        self.assertTrue((lead.qualification_data or {}).get("lead_notification_dry_run"))
        send_mock.assert_not_called()

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=False,
    )
    def test_c_real_success_sends_email_and_marks_sent_at(self):
        lead = _ready_lead(self.tenant)
        result = LeadNotificationService().notify(lead)
        lead.refresh_from_db()
        self.assertTrue(result.success)
        self.assertFalse(result.dry_run)
        self.assertFalse(result.skipped)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue((lead.qualification_data or {}).get(NOTIFICATION_SENT_KEY))
        self.assertFalse((lead.qualification_data or {}).get(NOTIFICATION_DRY_RUN_KEY))

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=False,
    )
    def test_d_real_replay_does_not_duplicate_email(self):
        lead = _ready_lead(self.tenant)
        first = LeadNotificationService().notify(lead)
        second = LeadNotificationService().notify(lead)
        self.assertFalse(first.skipped)
        self.assertTrue(second.skipped)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=False,
    )
    def test_e_smtp_failure_does_not_mark_sent_and_is_retryable(self):
        lead = _ready_lead(self.tenant)
        event = OutboxEvent.objects.create(
            tenant=self.tenant,
            event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
            aggregate_type="LeadDraft",
            aggregate_id=str(lead.pk),
            event_id=uuid.uuid4(),
            deduplication_key=f"lead-failure-{lead.pk}",
            available_at=timezone.now(),
            payload={"schema_version": SCHEMA_VERSION, "lead_draft_id": lead.pk},
            status=OutboxEvent.Status.PENDING,
        )
        with patch("leads.services.lead_notification.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            result = LeadQualifiedHandler().process(event)
        lead.refresh_from_db()
        self.assertEqual(result.status, "retryable_failure")
        self.assertFalse((lead.qualification_data or {}).get(NOTIFICATION_SENT_KEY))
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=False,
    )
    def test_f_retry_after_failure_sends_once_and_marks_sent_at(self):
        lead = _ready_lead(self.tenant)
        event = OutboxEvent.objects.create(
            tenant=self.tenant,
            event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
            aggregate_type="LeadDraft",
            aggregate_id=str(lead.pk),
            event_id=uuid.uuid4(),
            deduplication_key=f"lead-retry-success-{lead.pk}",
            available_at=timezone.now(),
            payload={"schema_version": SCHEMA_VERSION, "lead_draft_id": lead.pk},
            status=OutboxEvent.Status.PENDING,
        )
        with patch("leads.services.lead_notification.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            failed = LeadQualifiedHandler().process(event)
        self.assertEqual(failed.status, "retryable_failure")
        succeeded = LeadQualifiedHandler().process(event)
        lead.refresh_from_db()
        self.assertEqual(succeeded.status, "succeeded")
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue((lead.qualification_data or {}).get(NOTIFICATION_SENT_KEY))

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_g_dry_run_is_not_confused_with_delivered(self):
        lead = _ready_lead(self.tenant)
        dry = LeadNotificationService().notify(lead)
        lead.refresh_from_db()
        self.assertTrue(dry.dry_run)
        self.assertFalse(dry.skipped)
        self.assertTrue((lead.qualification_data or {}).get(NOTIFICATION_DRY_RUN_KEY))
        self.assertFalse((lead.qualification_data or {}).get(NOTIFICATION_SENT_KEY))

        with override_settings(LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False):
            real = LeadNotificationService().notify(lead)
        lead.refresh_from_db()
        self.assertTrue(real.success)
        self.assertFalse(real.dry_run)
        self.assertFalse(real.skipped)
        self.assertTrue((lead.qualification_data or {}).get(NOTIFICATION_SENT_KEY))
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_h_recipient_remains_multi_tenant(self):
        lead_scb = _ready_lead(self.tenant)
        lead_other = _ready_lead(self.other_tenant, session_id="other-session")
        LeadNotificationService().notify(lead_scb)
        LeadNotificationService().notify(lead_other)
        self.assertEqual(mail.outbox[0].to, ["comercial@smartcontrolbrasil.com.br"])
        self.assertEqual(mail.outbox[1].to, ["contato@outro-tenant.example"])

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=False,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=True,
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=False,
    )
    def test_disabled_outbox_handler_skips_without_retry_loop(self):
        lead = _ready_lead(self.tenant)
        event = OutboxEvent.objects.create(
            tenant=self.tenant,
            event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
            aggregate_type="LeadDraft",
            aggregate_id=str(lead.pk),
            event_id=uuid.uuid4(),
            deduplication_key=f"lead-disabled-{lead.pk}",
            available_at=timezone.now(),
            payload={"schema_version": SCHEMA_VERSION, "lead_draft_id": lead.pk},
            status=OutboxEvent.Status.PENDING,
        )
        result = LeadQualifiedHandler().process(event)
        self.assertEqual(result.status, "skipped")
        self.assertTrue(result.metadata["email"]["skipped"])

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=True,
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=False,
    )
    def test_dry_run_outbox_succeeds_without_sent_at(self):
        lead = _ready_lead(self.tenant)
        event = OutboxEvent.objects.create(
            tenant=self.tenant,
            event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
            aggregate_type="LeadDraft",
            aggregate_id=str(lead.pk),
            event_id=uuid.uuid4(),
            deduplication_key=f"lead-dry-run-{lead.pk}",
            available_at=timezone.now(),
            payload={"schema_version": SCHEMA_VERSION, "lead_draft_id": lead.pk},
            status=OutboxEvent.Status.PENDING,
        )
        result = LeadQualifiedHandler().process(event)
        lead.refresh_from_db()
        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.metadata["email"]["dry_run"])
        self.assertFalse((lead.qualification_data or {}).get(NOTIFICATION_SENT_KEY))
        self.assertTrue((lead.qualification_data or {}).get(NOTIFICATION_DRY_RUN_KEY))
