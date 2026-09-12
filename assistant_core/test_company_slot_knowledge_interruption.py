"""Regressão: slot name_or_company + interrupção por consulta técnica durante coleta."""

from __future__ import annotations

from django.test import TestCase

from assistant_core.consultative_policy import COLLECTION_ACTIVE_KEY
from assistant_core.qualification import (
    infer_pending_field_values,
    is_valid_company,
    is_valid_name,
    message_fills_pending_slot,
)
from assistant_core.services import LiviaDecisionService
from conversations.models import Conversation
from leads.models import LeadDraft
from leads.services.commercial import QualificationService, name_or_company_satisfied
from tenants.models import AssistantProfile, Tenant


class CompanySlotKnowledgeInterruptionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="scb-company-slot",
            domain="scb-company.example",
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
        assert lead is not None
        lead.refresh_from_db()
        return lead

    def test_lowercase_company_valid(self):
        for msg in ("smart control brasil", "Smart Control Brasil", "SMART CONTROL BRASIL", "Grupo Mecanismo", "RLMarmores"):
            with self.subTest(msg=msg):
                self.assertTrue(is_valid_company(msg), msg)
                inferred = infer_pending_field_values(msg, "name_or_company")
                self.assertTrue(inferred.get("company") or inferred.get("name"), msg)

    def test_operational_context_not_company(self):
        for msg in ("galpão", "3000 metros quadrados", "limpeza", "quero orçamento", "um condomínio"):
            with self.subTest(msg=msg):
                self.assertFalse(is_valid_company(msg), msg)
                self.assertFalse(message_fills_pending_slot(msg, "name_or_company"), msg)

    def test_deferral_phrase_not_company(self):
        for msg in ("ja passei", "já passei", "ja falei"):
            with self.subTest(msg=msg):
                self.assertFalse(is_valid_company(msg))
                self.assertFalse(is_valid_name(msg))
                self.assertFalse(message_fills_pending_slot(msg, "name_or_company"))

    def test_company_slot_advances_after_smart_control_brasil(self):
        conversation = self._conversation("company-slot-loop")
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            need_summary="limpeza profissional automatizada",
            qualification_data={COLLECTION_ACTIVE_KEY: True},
        )
        outcome = self.qualification.qualify_from_message(
            conversation=conversation,
            message="smart control brasil",
            history=[],
        )
        lead = outcome.lead_draft
        self.assertTrue(name_or_company_satisfied(lead))
        self.assertNotIn("name_or_company", outcome.missing_fields)
        self.assertNotIn("company", outcome.invalid_fields)
        missing = outcome.missing_fields
        self.assertIn("phone", missing)
        self.assertIn("email", missing)

    def test_ja_passei_does_not_loop_when_company_already_filled(self):
        conversation = self._conversation("ja-passei-loop")
        LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            need_summary="limpeza de galpão",
            company="smart control brasil",
            qualification_data={COLLECTION_ACTIVE_KEY: True},
        )
        outcome = self.qualification.qualify_from_message(
            conversation=conversation,
            message="ja passei",
            history=[],
        )
        self.assertNotIn("name_or_company", outcome.missing_fields)
        self.assertEqual(outcome.lead_draft.company, "smart control brasil")

    def test_full_smoke_multi_turn_sequence(self):
        conversation = self._conversation("full-company-smoke")
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

        turn2 = self.service.generate_reply(history, "Galpão de 3000 metros quadrados", conversation=conversation)
        history.extend(
            [
                {"role": "user", "content": "Galpão de 3000 metros quadrados"},
                {"role": "assistant", "content": turn2.reply},
            ]
        )
        self.assertNotIn("nome real da empresa", turn2.reply.lower())
        self.assertNotIn("telefone ficou incompleto", turn2.reply.lower())

        turn3 = self.service.generate_reply(
            history,
            "me fale mais sobre o Duno",
            conversation=conversation,
            knowledge_context=self.kb_duno,
        )
        history.extend(
            [
                {"role": "user", "content": "me fale mais sobre o Duno"},
                {"role": "assistant", "content": turn3.reply},
            ]
        )
        lowered3 = turn3.reply.lower()
        self.assertIn("duno", lowered3)
        self.assertNotIn("qual é o ambiente e o tipo de piso", lowered3)
        self.assertNotIn("qual é o tipo de piso", lowered3)

        turn4 = self.service.generate_reply(history, "quero um orçamento", conversation=conversation)
        history.extend(
            [
                {"role": "user", "content": "quero um orçamento"},
                {"role": "assistant", "content": turn4.reply},
            ]
        )
        lead = self._lead(conversation)
        self.assertTrue((lead.qualification_data or {}).get(COLLECTION_ACTIVE_KEY))

        turn5 = self.service.generate_reply(history, "smart control brasil", conversation=conversation)
        lead.refresh_from_db()
        self.assertTrue(name_or_company_satisfied(lead))
        self.assertNotIn("nome real da empresa", turn5.reply.lower())
        missing = QualificationService().missing_fields(lead)
        self.assertIn("phone", missing)
        self.assertIn("email", missing)

        turn6 = self.service.generate_reply(
            history
            + [
                {"role": "user", "content": "smart control brasil"},
                {"role": "assistant", "content": turn5.reply},
            ],
            "ja passei",
            conversation=conversation,
        )
        self.assertNotIn("nome real da empresa", turn6.reply.lower())
        lead.refresh_from_db()
        self.assertEqual(lead.company, "smart control brasil")
