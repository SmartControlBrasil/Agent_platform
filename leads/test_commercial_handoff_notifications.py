"""Fase 16B.3 — handoff + e-mail comercial completo (sem SMTP real)."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from assistant_core.consultative_policy import mark_collection_active
from assistant_core.services.livia_decision import LiviaDecisionService
from assistant_core.summary import (
    MAX_COMMERCIAL_TRANSCRIPT_CHARS,
    MAX_TRANSCRIPT_CHARS,
    MAX_TRANSCRIPT_TURNS,
    build_commercial_notification_transcript,
    build_conversation_transcript,
    build_lead_notification_body,
    build_lead_notification_subject,
)
from conversations.models import Conversation, HandoffRequest, Message
from integrations.models import OutboxEvent
from integrations.outbox.handlers import HandoffCreatedHandler, LeadQualifiedHandler
from integrations.outbox.payloads import SCHEMA_VERSION
from leads.models import LeadDraft
from leads.services.commercial import is_ready_for_commercial_notification
from leads.services.handoff import HandoffService
from leads.services.handoff_notification import HandoffNotificationService
from leads.services.lead_notification import LeadNotificationService
from tenants.models import AssistantProfile, Tenant


NOTIFY_SETTINGS = dict(
    LIVIA_AI_ENABLED=False,
    LIVIA_RAG_ENABLED=False,
    LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
    LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
    LIVIA_HANDOFF_NOTIFICATIONS_ENABLED=True,
    LIVIA_HANDOFF_NOTIFICATIONS_DRY_RUN=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    SMART360_LEAD_DISPATCH_ENABLED=False,
    SMART360_LEAD_DISPATCH_DRY_RUN=False,
)


@override_settings(**NOTIFY_SETTINGS)
class CommercialHandoffNotificationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Tenant Alfa", slug="tenant-alfa")
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            notification_email="comercial@tenant-alfa.example",
        )
        self.other = Tenant.objects.create(name="Tenant Beta", slug="tenant-beta")
        AssistantProfile.objects.create(
            tenant=self.other,
            name="Lívia",
            notification_email="comercial@tenant-beta.example",
        )
        self.service = LiviaDecisionService()
        self.history: list[dict[str, str]] = []
        self.conversation = Conversation.objects.create(
            tenant=self.tenant,
            session_id=str(uuid.uuid4()),
            source_page="https://www.tenant-alfa.example/contato",
        )

    def turn(self, text: str, *, conversation=None, history=None) -> str:
        conversation = conversation or self.conversation
        hist = self.history if history is None else history
        result = self.service.generate_reply(hist, text, conversation=conversation, knowledge_context="")
        hist.extend([{"role": "user", "content": text}, {"role": "assistant", "content": result.reply}])
        return result.reply

    def lead(self, conversation=None):
        return LeadDraft.objects.filter(conversation=conversation or self.conversation).first()

    def _outbox_lead(self, lead):
        return OutboxEvent.objects.create(
            tenant=lead.tenant,
            event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
            aggregate_type="LeadDraft",
            aggregate_id=str(lead.pk),
            event_id=uuid.uuid4(),
            deduplication_key=f"lead-16b3-{lead.pk}-{uuid.uuid4()}",
            available_at=timezone.now(),
            payload={"schema_version": SCHEMA_VERSION, "lead_draft_id": lead.pk},
            status=OutboxEvent.Status.PENDING,
        )

    def _outbox_handoff(self, handoff):
        return OutboxEvent.objects.create(
            tenant=handoff.tenant,
            event_type=OutboxEvent.EventType.HANDOFF_CREATED,
            aggregate_type="HandoffRequest",
            aggregate_id=str(handoff.pk),
            event_id=uuid.uuid4(),
            deduplication_key=f"handoff-16b3-{handoff.pk}-{uuid.uuid4()}",
            available_at=timezone.now(),
            payload={"schema_version": SCHEMA_VERSION, "handoff_id": handoff.pk},
            status=OutboxEvent.Status.PENDING,
        )

    def test_a_technical_question_sends_no_email(self):
        self.turn("Como funciona um CLP?")
        self.assertEqual(len(mail.outbox), 0)
        lead = self.lead()
        if lead is not None:
            self.assertFalse(is_ready_for_commercial_notification(lead))
            LeadNotificationService().notify(lead)
        self.assertEqual(len(mail.outbox), 0)

    def test_b_exploratory_interest_persists_without_email(self):
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            need_summary="Tenho três máquinas que preciso automatizar.",
        )
        mark_collection_active(lead, reason="semantic_commercial_interest", intent_type="commercial_interest")
        self.assertTrue((lead.qualification_data or {}).get("commercial_intent"))
        self.assertFalse(is_ready_for_commercial_notification(lead))
        result = LeadNotificationService().notify(lead)
        self.assertTrue(result.skipped)
        self.assertEqual(len(mail.outbox), 0)

    def test_c_budget_plus_phone_does_not_require_email(self):
        self.turn("Quero orçamento para automação industrial do galpão.")
        self.turn("Meu nome é João.")
        self.turn("Meu telefone é 11999999999.")
        lead = self.lead()
        self.assertTrue(is_ready_for_commercial_notification(lead))
        self.assertEqual(lead.email, "")
        LeadQualifiedHandler().process(self._outbox_lead(lead))
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("João", body)
        self.assertIn("11999999999", body)
        self.assertIn("E-mail: Não informado", body)
        self.assertIn("Novo lead Lívia — Orçamento", mail.outbox[0].subject)

    def test_d_budget_plus_email_without_phone(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id=str(uuid.uuid4()))
        history = []
        self.turn("Quero orçamento para automação industrial do galpão.", conversation=conversation, history=history)
        self.turn("Meu nome é João.", conversation=conversation, history=history)
        self.turn("Meu e-mail é joao@empresa.com.br", conversation=conversation, history=history)
        lead = self.lead(conversation)
        self.assertTrue(is_ready_for_commercial_notification(lead))
        self.assertFalse(str(lead.phone or "").strip())
        LeadQualifiedHandler().process(self._outbox_lead(lead))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("joao@empresa.com.br", mail.outbox[0].body)
        self.assertIn("Telefone: Não informado", mail.outbox[0].body)

    def test_e_all_fields_appear(self):
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            name="João Silva",
            phone="11999999999",
            email="joao@xpto.com.br",
            company="XPTO Automação",
            need_summary="Preciso automatizar três painéis no galpão.",
            status=LeadDraft.Status.QUALIFIED,
        )
        mark_collection_active(lead, reason="explicit_quote")
        body = build_lead_notification_body(lead, timestamp="01/10/2026 19:00")
        self.assertIn("Nome: João Silva", body)
        self.assertIn("Telefone: 11999999999", body)
        self.assertIn("E-mail: joao@xpto.com.br", body)
        self.assertIn("Empresa: XPTO Automação", body)

    def test_f_missing_company_is_not_invented(self):
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            name="João",
            phone="11999999999",
            need_summary="Preciso de automação industrial para o galpão.",
            status=LeadDraft.Status.QUALIFIED,
        )
        mark_collection_active(lead, reason="explicit_quote")
        body = build_lead_notification_body(lead)
        self.assertIn("Empresa: Não informado", body)
        self.assertNotIn("Empresa: XPTO", body)

    def test_g_human_request_creates_handoff_without_duplicate_email(self):
        self.turn("Quero falar com um atendente sobre automação industrial do galpão.")
        self.turn("Meu nome é João.")
        self.turn("Meu telefone é 11999999999.")
        lead = self.lead()
        handoff = HandoffRequest.objects.get(conversation=self.conversation)
        self.assertTrue(lead.qualification_data.get("commercial_intent"))
        LeadQualifiedHandler().process(self._outbox_lead(lead))
        HandoffCreatedHandler().process(self._outbox_handoff(handoff))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Solicitação de atendente", mail.outbox[0].subject + mail.outbox[0].body)

    def test_h_budget_and_human_same_cycle_one_email(self):
        self.turn("Quero orçamento para automação industrial do galpão.")
        self.turn("Quero falar com um atendente.")
        self.turn("João")
        self.turn("11999999999")
        lead = self.lead()
        handoff = HandoffRequest.objects.filter(conversation=self.conversation).first()
        self.assertIsNotNone(handoff)
        LeadQualifiedHandler().process(self._outbox_lead(lead))
        HandoffCreatedHandler().process(self._outbox_handoff(handoff))
        self.assertEqual(len(mail.outbox), 1)

    def test_i_repeated_budget_one_notification(self):
        self.turn("Quero orçamento para automação industrial do galpão.")
        self.turn("Quero orçamento para automação industrial do galpão.")
        self.turn("Preciso de cotação.")
        self.turn("Meu nome é João.")
        self.turn("11999999999")
        lead = self.lead()
        first = LeadQualifiedHandler().process(self._outbox_lead(lead))
        second = LeadQualifiedHandler().process(self._outbox_lead(lead))
        self.assertEqual(first.status, "succeeded")
        self.assertTrue(second.metadata["email"]["skipped"])
        self.assertEqual(len(mail.outbox), 1)

    def test_j_conversation_continues_after_email_without_new_send(self):
        self.turn("Quero orçamento para automação industrial do galpão.")
        self.turn("Meu nome é João.")
        self.turn("11999999999")
        lead = self.lead()
        LeadQualifiedHandler().process(self._outbox_lead(lead))
        self.assertEqual(len(mail.outbox), 1)
        reply = self.turn("Vocês trabalham com Mitsubishi?")
        self.assertTrue(reply.strip())
        self.assertNotIn("já encaminhei", reply.lower())
        LeadNotificationService().notify(self.lead())
        self.assertEqual(len(mail.outbox), 1)

    def test_k_later_company_update_does_not_resend(self):
        self.turn("Quero orçamento para automação industrial do galpão.")
        self.turn("Meu nome é João.")
        self.turn("11999999999")
        lead = self.lead()
        LeadQualifiedHandler().process(self._outbox_lead(lead))
        self.turn("Minha empresa é XPTO.")
        lead.refresh_from_db()
        self.assertIn("XPTO", lead.company)
        LeadNotificationService().notify(lead)
        self.assertEqual(len(mail.outbox), 1)

    def test_l_transcript_includes_all_user_assistant_turns(self):
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            name="João",
            phone="11999999999",
            need_summary="Preciso de automação industrial para o galpão.",
            status=LeadDraft.Status.QUALIFIED,
        )
        mark_collection_active(lead, reason="explicit_quote")
        pairs = [
            ("Olá", "Olá! Sou a Lívia."),
            ("Quero orçamento", "Certo. Como posso te chamar?"),
            ("João", "Qual é o melhor telefone ou WhatsApp?"),
            ("Mensagem extra do cliente", "Resposta extra da Lívia"),
        ]
        for user, assistant in pairs:
            Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content=user)
            Message.objects.create(conversation=self.conversation, role=Message.Role.ASSISTANT, content=assistant)
        body = build_lead_notification_body(lead)
        self.assertIn("Cliente: Olá", body)
        self.assertIn("Cliente: Mensagem extra do cliente", body)
        self.assertIn("Lívia: Resposta extra da Lívia", body)
        self.assertLess(body.index("Cliente: Olá"), body.index("Cliente: Mensagem extra do cliente"))

    def test_m_system_message_excluded(self):
        Message.objects.create(conversation=self.conversation, role=Message.Role.SYSTEM, content="prompt interno secreto")
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="Quero orçamento")
        transcript = build_commercial_notification_transcript(self.conversation)
        self.assertNotIn("prompt interno secreto", transcript)
        self.assertIn("Cliente: Quero orçamento", transcript)

    def test_n_long_conversation_is_not_silently_cut_at_14k(self):
        chunk = "Preciso detalhar o projeto de automação do galpão " * 20
        for index in range(12):
            Message.objects.create(
                conversation=self.conversation,
                role=Message.Role.USER,
                content=f"{index} {chunk}",
            )
            Message.objects.create(
                conversation=self.conversation,
                role=Message.Role.ASSISTANT,
                content=f"ok {index} {chunk}",
            )
        limited = build_conversation_transcript(self.conversation)
        commercial = build_commercial_notification_transcript(self.conversation)
        self.assertGreater(len(commercial), MAX_TRANSCRIPT_CHARS)
        self.assertIn("Cliente: 0 ", commercial)
        self.assertNotIn("[Truncamento excepcional]", commercial)
        self.assertTrue(
            "omitidas para legibilidade" in limited or "histórico truncado" in limited or len(limited) <= MAX_TRANSCRIPT_CHARS + 200
        )

    def test_o_more_than_80_turns_keeps_early_messages_in_commercial(self):
        for index in range(90):
            Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content=f"turno-user-{index}")
            Message.objects.create(conversation=self.conversation, role=Message.Role.ASSISTANT, content=f"turno-livia-{index}")
        limited = build_conversation_transcript(self.conversation)
        commercial = build_commercial_notification_transcript(self.conversation)
        self.assertIn("Cliente: turno-user-0", commercial)
        self.assertIn("Cliente: turno-user-89", commercial)
        self.assertNotIn("Cliente: turno-user-0", limited)
        self.assertGreater(len(self.conversation.messages.exclude(role=Message.Role.SYSTEM)), MAX_TRANSCRIPT_TURNS)

    def test_p_smtp_failure_keeps_domain_and_retries(self):
        self.turn("Quero orçamento para automação industrial do galpão.")
        self.turn("João")
        self.turn("11999999999")
        lead = self.lead()
        event = self._outbox_lead(lead)
        with patch("leads.services.lead_notification.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            result = LeadQualifiedHandler().process(event)
        lead.refresh_from_db()
        self.assertEqual(result.status, "retryable_failure")
        self.assertEqual(lead.status, LeadDraft.Status.QUALIFIED)
        self.assertFalse((lead.qualification_data or {}).get("lead_notification_sent_at"))
        self.assertEqual(LeadDraft.objects.filter(pk=lead.pk).count(), 1)

    def test_q_retry_after_failure_sends_once(self):
        self.turn("Quero orçamento para automação industrial do galpão.")
        self.turn("João")
        self.turn("11999999999")
        lead = self.lead()
        event = self._outbox_lead(lead)
        with patch("leads.services.lead_notification.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            failed = LeadQualifiedHandler().process(event)
        self.assertEqual(failed.status, "retryable_failure")
        succeeded = LeadQualifiedHandler().process(event)
        self.assertEqual(succeeded.status, "succeeded")
        self.assertEqual(len(mail.outbox), 1)

    def test_r_tenant_isolation(self):
        conv_b = Conversation.objects.create(tenant=self.other, session_id=str(uuid.uuid4()), source_page="https://beta.example")
        hist_b = []
        self.turn("Quero orçamento para automação industrial do galpão.", conversation=self.conversation, history=self.history)
        self.turn("João Alfa", conversation=self.conversation, history=self.history)
        self.turn("11999999999", conversation=self.conversation, history=self.history)
        self.turn("Quero orçamento para automação industrial do galpão.", conversation=conv_b, history=hist_b)
        self.turn("Maria Beta", conversation=conv_b, history=hist_b)
        self.turn("11988887777", conversation=conv_b, history=hist_b)
        lead_a = self.lead(self.conversation)
        lead_b = self.lead(conv_b)
        LeadNotificationService().notify(lead_a)
        LeadNotificationService().notify(lead_b)
        self.assertEqual(mail.outbox[0].to, ["comercial@tenant-alfa.example"])
        self.assertEqual(mail.outbox[1].to, ["comercial@tenant-beta.example"])
        self.assertIn("João Alfa", mail.outbox[0].body)
        self.assertNotIn("João Alfa", mail.outbox[1].body)
        self.assertIn("Maria Beta", mail.outbox[1].body)

    def test_s_subject_uses_intent_and_party(self):
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            name="João Silva",
            company="XPTO Automação",
            phone="11999999999",
            need_summary="Preciso de orçamento para automação industrial.",
            status=LeadDraft.Status.QUALIFIED,
        )
        mark_collection_active(lead, reason="explicit_quote")
        self.assertEqual(
            build_lead_notification_subject(lead),
            "Novo lead Lívia — Orçamento — XPTO Automação",
        )
        lead.company = ""
        lead.save(update_fields=["company"])
        self.assertEqual(build_lead_notification_subject(lead), "Novo lead Lívia — Orçamento — João Silva")
        lead.name = ""
        lead.save(update_fields=["name"])
        self.assertEqual(build_lead_notification_subject(lead), "Novo lead Lívia — Orçamento")

    def test_t_logs_do_not_include_transcript_or_visitor_pii(self):
        self.turn("Quero orçamento para automação industrial do galpão.")
        self.turn("Meu nome é João.")
        self.turn("11999999999")
        lead = self.lead()
        with self.assertLogs("leads.services.lead_notification", level="INFO") as captured:
            LeadNotificationService().notify(lead)
        joined = "\n".join(captured.output)
        self.assertNotIn("11999999999", joined)
        self.assertNotIn("CONVERSA COMPLETA", joined)
        self.assertNotIn("Cliente: Quero orçamento", joined)

    def test_human_without_contact_does_not_email(self):
        self.turn("Quero falar com um atendente.")
        handoff = HandoffRequest.objects.get(conversation=self.conversation)
        result = HandoffNotificationService().notify(handoff)
        self.assertTrue(result.skipped)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse((handoff.metadata or {}).get("handoff_notification_sent_at"))

    def test_exceptional_commercial_limit_is_declared(self):
        oversized = "x" * 18000
        for index in range(15):
            Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content=f"{index}-{oversized}")
        transcript = build_commercial_notification_transcript(self.conversation)
        self.assertIn("[Truncamento excepcional]", transcript)
