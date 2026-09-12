"""Fase 4 — finalização do lead e notificação comercial."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from assistant_core.consultative_policy import COLLECTION_ACTIVE_KEY, mark_collection_active
from assistant_core.continuity_policy import OFFER, STATUS_KEY
from assistant_core.services.livia_decision import LiviaDecisionService
from assistant_core.summary import (
    build_conversation_transcript,
    build_lead_notification_body,
    build_lead_notification_subject,
    build_lead_notification_summary_text,
)
from conversations.models import Conversation, Message
from integrations.models import OutboxEvent
from integrations.outbox.handlers import LeadQualifiedHandler
from integrations.outbox.payloads import SCHEMA_VERSION
from integrations.outbox.service import enqueue_lead_qualified
from leads.models import LeadDraft
from leads.services.commercial import QualificationService, is_ready_for_commercial_notification
from leads.services.lead_capture import LeadCaptureService
from leads.services.lead_notification import LeadNotificationService
from tenants.models import AssistantProfile, Tenant

B2B_NEED = "Preciso automatizar o controle de temperatura de três câmaras na empresa."
KB = "[KNOWLEDGE_BASE]\nConteúdo:\nO controle de temperatura das câmaras exige avaliar sensores e cargas.\n[/KNOWLEDGE_BASE]"


def _activate_commercial_lead(lead: LeadDraft, *, reason: str = "continuity_offer_accepted") -> LeadDraft:
    mark_collection_active(lead, reason=reason)
    lead.refresh_from_db()
    return lead


def _complete_lead(
    tenant,
    *,
    name="Carlos Silva",
    phone="11999998888",
    email="carlos@example.com",
    need_summary=B2B_NEED,
    company="",
    reason="continuity_offer_accepted",
    session_id=None,
):
    conversation = Conversation.objects.create(
        tenant=tenant,
        session_id=session_id or str(uuid.uuid4()),
        source_page="https://www.smartcontrolbrasil.com.br/contato",
    )
    lead = LeadDraft.objects.create(
        tenant=tenant,
        conversation=conversation,
        name=name,
        phone=phone,
        email=email,
        company=company,
        need_summary=need_summary,
        status=LeadDraft.Status.QUALIFIED,
        qualification_status=LeadDraft.QualificationStatus.QUALIFIED,
        qualification_data={STATUS_KEY: "accepted"},
    )
    return _activate_commercial_lead(lead, reason=reason)


@override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
class LeadFinalizationTests(TestCase):
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
        self.service = QualificationService()
        self.capture = LeadCaptureService()

    def test_a_complete_lead_generates_outbox_event(self):
        lead = _complete_lead(self.tenant)
        self.capture._dispatch_webhook_lead_qualified(lead)
        self.assertEqual(
            OutboxEvent.objects.filter(
                event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
                aggregate_id=str(lead.pk),
            ).count(),
            1,
        )

    def test_b_relational_name_alone_does_not_notify(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="relational-only")
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            name="Marcelo",
            need_summary=B2B_NEED,
            qualification_data={"relational_name_captured": True},
        )
        self.assertFalse(is_ready_for_commercial_notification(lead))
        self.capture.capture_from_message(conversation=conversation, message="Marcelo", history=[])
        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_c_incomplete_lead_does_not_notify(self):
        lead = _complete_lead(self.tenant, email="")
        self.assertFalse(is_ready_for_commercial_notification(lead))
        with patch.object(self.capture, "_dispatch_webhook_lead_qualified") as dispatch:
            self.capture.capture_from_message(
                conversation=lead.conversation,
                message="11999998888",
                history=[],
            )
            dispatch.assert_not_called()

    def test_d_notification_valid_without_company(self):
        lead = _complete_lead(self.tenant)
        self.assertTrue(is_ready_for_commercial_notification(lead))

    def test_e_company_present_in_email_body(self):
        lead = _complete_lead(self.tenant, company="Empresa XYZ")
        Message.objects.create(conversation=lead.conversation, role=Message.Role.USER, content="Preciso de automação.")
        body = build_lead_notification_body(lead, timestamp="12/09/2026 10:00")
        self.assertIn("Empresa: Empresa XYZ", body)

    def test_f_subject_follows_pattern(self):
        lead = _complete_lead(self.tenant, name="Carlos", need_summary="Preciso de robô de limpeza para galpão.")
        subject = build_lead_notification_subject(lead)
        self.assertEqual(subject, "[Lívia] Novo atendimento — Carlos — Robô de limpeza")

    def test_g_transcript_has_client_and_livia_in_order(self):
        lead = _complete_lead(self.tenant)
        Message.objects.create(conversation=lead.conversation, role=Message.Role.USER, content="Preciso de automação.")
        Message.objects.create(conversation=lead.conversation, role=Message.Role.ASSISTANT, content="Posso ajudar com isso.")
        Message.objects.create(conversation=lead.conversation, role=Message.Role.SYSTEM, content="prompt interno")
        transcript = build_conversation_transcript(lead.conversation, lead_draft=lead)
        self.assertIn("Cliente:", transcript)
        self.assertIn("Lívia:", transcript)
        self.assertLess(transcript.index("Cliente:"), transcript.index("Lívia:"))
        self.assertNotIn("prompt interno", transcript)

    def test_h_summary_exists_and_is_not_full_transcript(self):
        lead = _complete_lead(self.tenant)
        Message.objects.create(conversation=lead.conversation, role=Message.Role.USER, content="Preciso automatizar três câmaras.")
        summary_text = build_lead_notification_summary_text(lead.conversation, lead_draft=lead)
        transcript = build_conversation_transcript(lead.conversation, lead_draft=lead)
        body = build_lead_notification_body(lead, timestamp="12/09/2026 10:00")
        self.assertIn("RESUMO DO ATENDIMENTO", body)
        self.assertIn("HISTÓRICO DA CONVERSA", body)
        self.assertIn(summary_text, body)
        self.assertNotEqual(summary_text.strip(), transcript.strip())

    def test_i_idempotent_outbox_and_notification(self):
        lead = _complete_lead(self.tenant)
        first_event, created = enqueue_lead_qualified(lead)
        second_event, created_again = enqueue_lead_qualified(lead)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first_event.pk, second_event.pk)
        with override_settings(
            LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
            LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=True,
        ):
            first = LeadNotificationService().notify(lead)
            second = LeadNotificationService().notify(lead)
        self.assertFalse(first.skipped)
        self.assertTrue(second.skipped)
        lead.refresh_from_db()
        self.assertFalse((lead.qualification_data or {}).get("lead_notification_sent_at"))
        self.assertTrue((lead.qualification_data or {}).get("lead_notification_dry_run_at"))

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=False,
    )
    def test_j_smtp_failure_keeps_lead_and_allows_retry(self):
        lead = _complete_lead(self.tenant)
        event = OutboxEvent.objects.create(
            tenant=self.tenant,
            event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
            aggregate_type="LeadDraft",
            aggregate_id=str(lead.pk),
            event_id=uuid.uuid4(),
            deduplication_key=f"lead-notify-{lead.pk}",
            available_at=timezone.now(),
            payload={"schema_version": SCHEMA_VERSION, "lead_draft_id": lead.pk},
            status=OutboxEvent.Status.PENDING,
        )
        with patch("leads.services.lead_notification.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            result = LeadQualifiedHandler().process(event)
        lead.refresh_from_db()
        self.assertEqual(result.status, "retryable_failure")
        self.assertEqual(lead.status, LeadDraft.Status.QUALIFIED)
        self.assertFalse((lead.qualification_data or {}).get("lead_notification_sent_at"))

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=True,
    )
    def test_k_retry_sends_once_without_duplicating_lead(self):
        lead = _complete_lead(self.tenant)
        event = OutboxEvent.objects.create(
            tenant=self.tenant,
            event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
            aggregate_type="LeadDraft",
            aggregate_id=str(lead.pk),
            event_id=uuid.uuid4(),
            deduplication_key=f"lead-retry-{lead.pk}",
            available_at=timezone.now(),
            payload={"schema_version": SCHEMA_VERSION, "lead_draft_id": lead.pk},
            status=OutboxEvent.Status.PENDING,
        )
        first = LeadQualifiedHandler().process(event)
        second = LeadQualifiedHandler().process(event)
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(second.metadata["email"]["skipped"], True)
        self.assertEqual(LeadDraft.objects.filter(pk=lead.pk).count(), 1)

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
        LIVIA_LEAD_NOTIFICATION_EMAIL="fallback@example.com",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=True,
    )
    def test_l_multi_tenant_recipient_resolution(self):
        lead_scb = _complete_lead(self.tenant)
        lead_other = _complete_lead(self.other_tenant, session_id="other-session")
        LeadNotificationService().notify(lead_scb)
        LeadNotificationService().notify(lead_other)
        self.assertEqual(mail.outbox[0].to, ["comercial@smartcontrolbrasil.com.br"])
        self.assertEqual(mail.outbox[1].to, ["contato@outro-tenant.example"])

    def test_m_system_messages_excluded_from_transcript(self):
        lead = _complete_lead(self.tenant)
        Message.objects.create(conversation=lead.conversation, role=Message.Role.SYSTEM, content="system prompt")
        Message.objects.create(conversation=lead.conversation, role=Message.Role.USER, content="Olá")
        transcript = build_conversation_transcript(lead.conversation, lead_draft=lead)
        self.assertIn("Cliente: Olá", transcript)
        self.assertNotIn("system prompt", transcript)

    def test_n_spontaneous_contacts_generate_same_notification_when_ready(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="spontaneous")
        lead = LeadDraft.objects.create(tenant=self.tenant, conversation=conversation, qualification_data={})
        mark_collection_active(lead, reason="explicit_quote")
        message = (
            "Sou Ana Silva, meu telefone é 11988887777, meu e-mail é ana@example.com "
            "e preciso automatizar três câmaras na empresa."
        )
        outcome = self.capture.capture_from_message(conversation=conversation, message=message, history=[])
        self.assertTrue(outcome.is_qualified)
        self.assertTrue(is_ready_for_commercial_notification(outcome.lead_draft))
        self.assertEqual(outcome.lead_draft.name, "Ana Silva")

    def test_o_relational_name_without_intent_does_not_enqueue(self):
        decision = LiviaDecisionService()
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="phase4-relational")
        history = []

        def turn(text):
            nonlocal history
            result = decision.generate_reply(history, text, conversation=conversation, knowledge_context=KB)
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": result.reply}])
            return result

        turn("O Duno consegue limpar um galpão?")
        turn("E consegue trabalhar várias horas?")
        turn("Como funciona o mapeamento?")
        turn("Marcelo")
        lead = LeadDraft.objects.filter(conversation=conversation).first()
        self.assertTrue(lead.name)
        self.assertFalse((lead.qualification_data or {}).get(COLLECTION_ACTIVE_KEY))
        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_p_continuity_acceptance_after_contacts_enqueues_notification(self):
        decision = LiviaDecisionService()
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="phase4-continuity")
        history = []

        def turn(text):
            nonlocal history
            result = decision.generate_reply(history, text, conversation=conversation, knowledge_context=KB)
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": result.reply}])
            return result

        self.assertIn(OFFER, turn(B2B_NEED).reply)
        turn("Pode registrar")
        turn("Carlos Silva")
        turn("11999998888")
        reply = turn("carlos@example.com").reply
        lead = LeadDraft.objects.get(conversation=conversation)
        self.assertTrue(is_ready_for_commercial_notification(lead))
        self.assertEqual(lead.status, LeadDraft.Status.QUALIFIED)
        self.assertEqual(
            OutboxEvent.objects.filter(
                event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
                aggregate_id=str(lead.pk),
            ).count(),
            1,
        )
        self.assertIn("continuidade", reply.lower())

    def test_email_body_structure(self):
        lead = _complete_lead(self.tenant, company="Empresa XYZ")
        Message.objects.create(conversation=lead.conversation, role=Message.Role.USER, content="Preciso de automação.")
        Message.objects.create(conversation=lead.conversation, role=Message.Role.ASSISTANT, content="Posso ajudar.")
        body = build_lead_notification_body(lead, timestamp="12/09/2026 10:00")
        for section in (
            "DADOS DO CONTATO",
            "ASSUNTO",
            "RESUMO DO ATENDIMENTO",
            "HISTÓRICO DA CONVERSA",
            "Cliente:",
            "Lívia:",
            "INFORMAÇÕES INTERNAS",
        ):
            self.assertIn(section, body)
        self.assertIn("Empresa: Empresa XYZ", body)

    def test_subject_without_confident_topic_uses_name_only(self):
        lead = _complete_lead(self.tenant, name="Ana", need_summary="Preciso de ajuda com meu projeto.")
        subject = build_lead_notification_subject(lead)
        self.assertEqual(subject, "[Lívia] Novo atendimento — Ana")
