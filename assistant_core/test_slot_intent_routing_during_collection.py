"""Slot intent routing durante coleta comercial — telefone vs contexto vs consulta técnica."""

from __future__ import annotations

from django.test import TestCase

from assistant_core.consultative_policy import COLLECTION_ACTIVE_KEY, decide_collection
from assistant_core.conversation_turns import TurnKind, classify_conversation_turn
from assistant_core.discovery import analyze_message
from assistant_core.qualification import (
    infer_pending_field_values,
    looks_like_invalid_phone,
    message_fills_pending_slot,
    message_is_plausible_phone_candidate,
)
from assistant_core.services import LiviaDecisionService
from conversations.models import Conversation, HandoffRequest
from leads.models import LeadDraft
from leads.services.commercial import QualificationService
from tenants.models import AssistantProfile, Tenant


class SlotIntentRoutingDuringCollectionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="scb-slot-intent",
            domain="scb-slot.example",
            is_active=True,
        )
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            business_domain="robótica e automação",
            short_description="Consultoria comercial e técnica.",
            human_handoff_enabled=True,
        )
        self.service = LiviaDecisionService()
        self.qualification = QualificationService()
        self.kb_duno = (
            "[KNOWLEDGE_BASE]\nConteúdo:\n"
            "Duno (HygiBot) é um robô autônomo de limpeza profissional para grandes áreas, "
            "com modos de lavar, varrer, aspirar e passar pano.\n"
            "[/KNOWLEDGE_BASE]"
        )

    def _conversation(self, session_id: str) -> Conversation:
        return Conversation.objects.create(tenant=self.tenant, session_id=session_id)

    def _lead(self, conversation: Conversation) -> LeadDraft:
        lead = LeadDraft.objects.filter(conversation=conversation).first()
        if lead is None:
            return conversation.lead_draft
        lead.refresh_from_db()
        return lead

    def _history(self, *user_messages: str) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for msg in user_messages:
            items.append({"role": "user", "content": msg})
            items.append({"role": "assistant", "content": "ok"})
        return items

    def test_galpao_area_not_phone_candidate(self):
        for msg in (
            "Galpão de 3000 mettros quadrados",
            "Galpão de 3000 metros quadrados",
            "área de 1500 m²",
            "temos 200 funcionários",
        ):
            with self.subTest(msg=msg):
                self.assertFalse(message_is_plausible_phone_candidate(msg), msg)
                self.assertFalse(looks_like_invalid_phone(msg), msg)
                self.assertFalse(infer_pending_field_values(msg, "phone_or_email"))

    def test_valid_phone_candidates(self):
        for msg in ("(11) 99999-8888", "11999998888", "+55 11 99999-8888"):
            with self.subTest(msg=msg):
                self.assertTrue(message_is_plausible_phone_candidate(msg))
                self.assertFalse(looks_like_invalid_phone(msg))

    def test_incomplete_phone_still_invalid(self):
        self.assertTrue(message_is_plausible_phone_candidate("meu telefone 11999"))
        self.assertTrue(looks_like_invalid_phone("meu telefone 11999"))

    def test_galpao_during_phone_slot_not_phone_invalid(self):
        conversation = self._conversation("galpao-phone-slot")
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            need_summary="limpeza profissional automatizada",
            name="Maria",
            qualification_data={COLLECTION_ACTIVE_KEY: True},
        )
        outcome = self.qualification.qualify_from_message(
            conversation=conversation,
            message="Galpão de 3000 metros quadrados",
            history=self._history("preciso de limpeza profissional e automatizada"),
        )
        self.assertNotIn("phone", outcome.invalid_fields)
        data = outcome.lead_draft.qualification_data or {}
        self.assertTrue(data.get("consultative_environment") or data.get("consultative_area_size"))

    def test_knowledge_question_not_slot_value_during_collection(self):
        msg = "me fale mais sobre o Duno"
        self.assertFalse(message_fills_pending_slot(msg, "name_or_company"))
        self.assertFalse(message_fills_pending_slot(msg, "phone_or_email"))

    def test_smoke_three_turn_sequence(self):
        conversation = self._conversation("smoke-slot-intent")
        history: list[dict[str, str]] = []

        turn1 = self.service.generate_reply(
            history,
            "preciso de limpeza profissional e automatizada",
            conversation=conversation,
            knowledge_context=self.kb_duno,
        )
        history.extend(
            [
                {"role": "user", "content": "preciso de limpeza profissional e automatizada"},
                {"role": "assistant", "content": turn1.reply},
            ]
        )
        self.assertNotIn("whatsapp", turn1.reply.lower())

        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        turn2 = self.service.generate_reply(
            history + [{"role": "user", "content": "quero um orçamento"}, {"role": "assistant", "content": "ok"}],
            "Galpão de 3000 metros quadrados",
            conversation=conversation,
        )
        self.assertNotIn("telefone ficou incompleto", turn2.reply.lower())
        self.assertNotIn("whatsapp", turn2.reply.lower())

        lead = self._lead(conversation)
        self.assertTrue((lead.qualification_data or {}).get(COLLECTION_ACTIVE_KEY))

        turn3 = self.service.generate_reply(
            history
            + [
                {"role": "user", "content": "quero um orçamento"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "Galpão de 3000 metros quadrados"},
                {"role": "assistant", "content": turn2.reply},
            ],
            "me fale mais sobre o Duno",
            conversation=conversation,
            knowledge_context=self.kb_duno,
        )
        lowered = turn3.reply.lower()
        self.assertIn("duno", lowered)
        self.assertNotIn("telefone ficou incompleto", lowered)
        self.assertNotIn("qual é o ambiente e o tipo de piso", lowered)
        lead.refresh_from_db()
        self.assertTrue((lead.qualification_data or {}).get(COLLECTION_ACTIVE_KEY))

    def test_knowledge_turn_classified_before_slot_during_collection(self):
        conversation = self._conversation("classify-knowledge")
        LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            need_summary="limpar galpão",
            name="João",
            qualification_data={COLLECTION_ACTIVE_KEY: True},
        )
        msg = "me fale mais sobre o Duno"
        discovery = analyze_message(msg)
        turn = classify_conversation_turn(
            current_message=msg,
            history=self._history("quero um orçamento", "limpar galpão"),
            conversation=conversation,
            discovery=discovery,
        )
        gate = decide_collection(current_message=msg, conversation=conversation, discovery=discovery)
        self.assertEqual(turn.kind, TurnKind.OTHER)
        self.assertFalse(gate.should_collect)
        self.assertEqual(gate.reason, "consultative_knowledge_during_collection")

    def test_human_handoff_still_priority_during_collection(self):
        conversation = self._conversation("handoff-priority")
        LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            need_summary="limpeza de galpão",
            qualification_data={COLLECTION_ACTIVE_KEY: True},
        )
        decision = self.service.generate_reply(
            self._history("quero um orçamento"),
            "quero falar com um vendedor",
            conversation=conversation,
        )
        self.assertIsNotNone(decision.handoff_request_id)
        self.assertEqual(HandoffRequest.objects.filter(conversation=conversation).count(), 1)

    def test_budget_still_starts_collection(self):
        conversation = self._conversation("budget-still-works")
        decision = self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        lead = self._lead(conversation)
        self.assertTrue((lead.qualification_data or {}).get(COLLECTION_ACTIVE_KEY))
        self.assertIn("qual é o seu nome", decision.reply.lower())

    def test_tenant_isolation(self):
        other = Tenant.objects.create(
            name="Outro",
            slug="other-slot-intent",
            domain="other-slot.example",
            is_active=True,
        )
        conv_a = self._conversation("tenant-a")
        conv_b = Conversation.objects.create(tenant=other, session_id="tenant-b")
        self.service.generate_reply([], "quero um orçamento", conversation=conv_a)
        self.service.generate_reply([], "quero um orçamento", conversation=conv_b)
        self.service.generate_reply(
            self._history("quero um orçamento"),
            "Galpão de 3000 metros quadrados",
            conversation=conv_b,
        )
        lead_a = LeadDraft.objects.filter(conversation=conv_a).first()
        lead_b = LeadDraft.objects.filter(conversation=conv_b).first()
        self.assertIsNotNone(lead_a)
        self.assertIsNotNone(lead_b)
        self.assertEqual(lead_a.tenant_id, self.tenant.id)
        self.assertEqual(lead_b.tenant_id, other.id)
