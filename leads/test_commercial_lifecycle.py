import json
import uuid
from unittest.mock import Mock, patch

from django.http import Http404
from django.test import TestCase, override_settings

from conversations.models import HandoffRequest
from integrations.models import OutboxEvent
from integrations.outbox.handlers import LeadQualifiedHandler
from integrations.outbox.service import enqueue_lead_qualified
from assistant_core.consultative_policy import mark_collection_active
from leads.models import LeadDraft
from leads.services.commercial import (
    CommercialReadinessService,
    FIELD_SOURCE_EXPLICIT,
    FIELD_SOURCE_INFERRED,
    QualificationFieldSpec,
    QualificationPolicy,
    QualificationService,
    merge_field_value,
)
from leads.services.handoff import HandoffService
from operations_portal.selectors import get_lead_detail
from tenants.models import AssistantProfile, Tenant
from tenants.services.human_handoff import build_human_handoff_payload
from conversations.models import Conversation


class CommercialLifecycleTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.other_tenant = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        self.conversation = Conversation.objects.create(tenant=self.tenant, session_id="session-a")
        self.other_conversation = Conversation.objects.create(tenant=self.other_tenant, session_id="session-b")
        self.service = QualificationService()

    def test_lead_draft_is_incremental_and_keeps_one_row_per_conversation(self):
        first = self.service.qualify_from_message(
            conversation=self.conversation,
            message="Quero um site com IA e dashboard comercial.",
            history=[],
        )
        second = self.service.qualify_from_message(
            conversation=self.conversation,
            message="Sou Maria da ACME e meu telefone é 11999998888, email maria@exemplo.com",
            history=[{"role": "user", "content": "Quero um site com IA e dashboard comercial."}],
        )

        self.assertEqual(first.lead_draft.pk, second.lead_draft.pk)
        self.assertEqual(LeadDraft.objects.filter(tenant=self.tenant, conversation=self.conversation).count(), 1)
        self.assertEqual(second.lead_draft.name, "Maria da ACME")
        self.assertEqual(second.lead_draft.phone, "11999998888")
        self.assertIn("site", second.lead_draft.need_summary.lower())
        self.assertEqual(second.lead_draft.qualification_status, LeadDraft.QualificationStatus.QUALIFIED)
        self.assertEqual(second.lead_draft.handoff_status, LeadDraft.HandoffStatus.READY)

    def test_retry_same_qualified_message_does_not_duplicate_lead_or_outbox(self):
        message = "Sou Maria da ACME, email maria@exemplo.com, telefone 11999998888 e preciso de automação industrial para uma linha."

        first = self.service.qualify_from_message(conversation=self.conversation, message=message, history=[])
        mark_collection_active(first.lead_draft, reason="explicit_quote")
        enqueue_lead_qualified(first.lead_draft)
        second = self.service.qualify_from_message(conversation=self.conversation, message=message, history=[])
        enqueue_lead_qualified(second.lead_draft)

        self.assertEqual(LeadDraft.objects.filter(conversation=self.conversation).count(), 1)
        self.assertEqual(OutboxEvent.objects.filter(event_type=OutboxEvent.EventType.LEAD_QUALIFIED, aggregate_id=str(first.lead_draft.pk)).count(), 1)

    def test_does_not_overwrite_explicit_contact_with_inferred_value(self):
        lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation, phone="11999998888", field_sources={"phone": FIELD_SOURCE_EXPLICIT})

        changed = merge_field_value(lead, "phone", "11888887777", source=FIELD_SOURCE_INFERRED)

        self.assertFalse(changed)
        self.assertEqual(lead.phone, "11999998888")

    def test_missing_phone_is_not_invented_from_call_me_tomorrow(self):
        outcome = self.service.qualify_from_message(
            conversation=self.conversation,
            message="Pode me chamar amanhã.",
            history=[],
        )

        self.assertEqual(outcome.lead_draft.phone, "")
        self.assertIn("phone", outcome.missing_fields)

    def test_email_and_phone_are_explicitly_collected_and_normalized(self):
        outcome = self.service.qualify_from_message(
            conversation=self.conversation,
            message="Meu email é MARIA@EXEMPLO.COM e telefone +55 (11) 99999-8888",
            history=[],
        )

        self.assertEqual(outcome.lead_draft.email, "maria@exemplo.com")
        self.assertEqual(outcome.lead_draft.phone, "11999998888")
        self.assertEqual(outcome.lead_draft.field_sources["email"], FIELD_SOURCE_EXPLICIT)
        self.assertEqual(outcome.lead_draft.field_sources["phone"], FIELD_SOURCE_EXPLICIT)

    def test_custom_tenant_specific_policy_controls_allowed_data_and_qualification(self):
        policy = QualificationPolicy(
            slug="stone-policy",
            required_fields=("name", "phone", "email", "need_summary", "custom:material"),
            custom_fields=(QualificationFieldSpec(key="material", label="material", required=True),),
        )

        incomplete = self.service.qualify_from_message(
            conversation=self.conversation,
            message="Sou Maria da ACME, email maria@exemplo.com, telefone 11999998888 e quero um site com IA para cozinha.",
            history=[],
            policy=policy,
        )
        complete = self.service.qualify_from_message(
            conversation=self.conversation,
            message="O material é granito",
            history=[{"role": "user", "content": "Sou Maria da ACME, email maria@exemplo.com, telefone 11999998888 e quero um site com IA para cozinha."}],
            policy=policy,
        )

        self.assertIn("custom:material", incomplete.missing_fields)
        self.assertEqual(complete.lead_draft.qualification_data, {"material": "granito"})
        self.assertEqual(complete.lead_draft.qualification_status, LeadDraft.QualificationStatus.QUALIFIED)

    def test_tenant_isolation_for_leads_and_portal_selector(self):
        outcome = self.service.qualify_from_message(
            conversation=self.conversation,
            message="Sou Maria, telefone 11999998888 e preciso de automação industrial.",
            history=[],
        )

        with self.assertRaises(Http404):
            get_lead_detail(outcome.lead_draft.pk, tenant=self.other_tenant)
        self.assertEqual(get_lead_detail(outcome.lead_draft.pk, tenant=self.tenant).tenant, self.tenant)

    def test_handoff_explicit_request_is_idempotent_and_tenant_scoped(self):
        lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation, name="Maria", phone="11999998888")
        service = HandoffService()

        first = service.create_or_update_handoff(self.conversation, lead_draft=lead, message="quero falar com uma pessoa")
        second = service.create_or_update_handoff(self.conversation, lead_draft=lead, message="me passa para um vendedor")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.handoff.pk, second.handoff.pk)
        self.assertEqual(HandoffRequest.objects.filter(tenant=self.tenant, conversation=self.conversation).count(), 1)
        lead.refresh_from_db()
        self.assertEqual(lead.handoff_status, LeadDraft.HandoffStatus.REQUESTED)
        self.assertEqual(OutboxEvent.objects.filter(event_type=OutboxEvent.EventType.HANDOFF_CREATED).count(), 1)

    def test_handoff_completion_updates_independent_handoff_state(self):
        lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation)
        result = HandoffService().create_or_update_handoff(self.conversation, lead_draft=lead, message="falar com atendente")

        HandoffService().mark_resolved(result.handoff)

        result.handoff.refresh_from_db()
        lead.refresh_from_db()
        self.assertEqual(result.handoff.handoff_state, HandoffRequest.HandoffState.COMPLETED)
        self.assertEqual(lead.handoff_status, LeadDraft.HandoffStatus.COMPLETED)

    def test_whatsapp_payload_uses_tenant_profile_and_disabled_tenant_is_safe(self):
        profile = AssistantProfile.objects.create(
            tenant=self.tenant,
            human_handoff_enabled=True,
            human_handoff_channel="whatsapp",
            handoff_whatsapp_number="+55 (11) 99999-8888",
            handoff_whatsapp_message="Olá, vim pela Lívia.",
        )
        other_profile = AssistantProfile.objects.create(tenant=self.other_tenant, human_handoff_enabled=False)
        handoff = HandoffRequest.objects.create(tenant=self.tenant, conversation=self.conversation)

        payload = build_human_handoff_payload(profile, handoff)

        self.assertTrue(payload["active"])
        self.assertIn("5511999998888", payload["url"])
        self.assertEqual(build_human_handoff_payload(other_profile, handoff), {"active": False})

    @override_settings(SMART360_LEAD_DISPATCH_ENABLED=False, SMART360_LEAD_DISPATCH_DRY_RUN=True)
    def test_smart360_dry_run_dispatch_does_not_corrupt_qualification_state(self):
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            name="Maria",
            phone="11999998888",
            need_summary="Preciso de automação industrial para atendimento.",
            status=LeadDraft.Status.QUALIFIED,
            qualification_status=LeadDraft.QualificationStatus.QUALIFIED,
            dispatch_status=LeadDraft.DispatchStatus.PENDING,
        )
        event, _ = enqueue_lead_qualified(lead)

        result = LeadQualifiedHandler().process(event)

        lead.refresh_from_db()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(lead.qualification_status, LeadDraft.QualificationStatus.QUALIFIED)
        self.assertNotEqual(lead.status, LeadDraft.Status.FAILED)
        self.assertIn(lead.dispatch_status, {LeadDraft.DispatchStatus.DRY_RUN, LeadDraft.DispatchStatus.DELIVERED})

    def test_commercial_readiness_is_distinct_from_other_readiness(self):
        partial = CommercialReadinessService().readiness(tenant=self.tenant)
        AssistantProfile.objects.create(
            tenant=self.tenant,
            human_handoff_enabled=True,
            human_handoff_channel="whatsapp",
            handoff_whatsapp_number="5511999998888",
        )
        ready = CommercialReadinessService().readiness(tenant=self.tenant)

        self.assertIn(partial.status, {"PARTIAL", "DEGRADED"})
        self.assertEqual(ready.status, "READY")
        self.assertIn("required_fields", ready.details)


class CommercialChatIdempotencyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a")

    def test_same_chat_request_retry_reuses_response_without_duplicate_lead_or_handoff(self):
        request_id = str(uuid.uuid4())
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "chat-retry",
            "request_id": request_id,
            "message": "Sou Maria da ACME, email maria@exemplo.com, telefone 11999998888 e preciso de automação industrial.",
        }

        first = self.client.post("/api/chat/", data=json.dumps(payload), content_type="application/json")
        second = self.client.post("/api/chat/", data=json.dumps(payload), content_type="application/json")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(LeadDraft.objects.count(), 1)
        self.assertLessEqual(HandoffRequest.objects.count(), 1)
        self.assertEqual(OutboxEvent.objects.filter(event_type=OutboxEvent.EventType.LEAD_QUALIFIED).count(), 1)
