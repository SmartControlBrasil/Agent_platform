"""Fase 5.2 — paridade conversacional e integridade de dados."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from assistant_core.continuity_policy import OFFER, STATUS_KEY, offer_after_guidance
from assistant_core.qualification import infer_pending_field_values
from assistant_core.relational_collection import pending_collection_fields
from assistant_core.services.chat_processing import _refine_response_with_ai_if_enabled
from assistant_core.services.livia_decision import LiviaDecisionService
from assistant_core.services.openai_grounded_conversation import OpenAIConversationResult
from conversations.models import Conversation
from leads.models import LeadDraft
from leads.services.commercial import QualificationService
from tenants.models import Tenant

NEED = "Preciso automatizar o controle de temperatura de três câmaras frigoríficas."
FOLLOWUP = "Como isso funcionaria na prática?"
GUIDANCE = (
    "Para três câmaras frigoríficas, o controle de temperatura exige sensores, "
    "controladores e avaliação das cargas térmicas de cada ambiente."
)
DIAGNOSTIC = "Para orientar melhor, qual é o modelo do equipamento de refrigeração?"
KB = "[KNOWLEDGE_BASE]\nConteúdo:\n" + GUIDANCE + "\nSensores e controladores para câmaras frigoríficas.\n[/KNOWLEDGE_BASE]"


@override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
class Phase52ContinuityTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Equipe Técnica", slug="phase52")
        self.conversation = Conversation.objects.create(tenant=self.tenant, session_id="phase52-continuity")
        self.service = LiviaDecisionService()
        self.history: list[dict[str, str]] = []

    def turn(self, message, knowledge=KB):
        reply = self.service.generate_reply(
            self.history, message, conversation=self.conversation, knowledge_context=knowledge,
        )
        self.history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply.reply}])
        return reply

    def lead(self):
        return LeadDraft.objects.get(conversation=self.conversation)

    def test_a_openai_inline_offers_after_grounded_guidance(self):
        self.history = [{"role": "user", "content": NEED}]
        with patch("assistant_core.services.chat_processing._can_refine_with_ai", return_value=True), patch(
            "assistant_core.services.openai_grounded_conversation.OpenAIGroundedConversationService.generate",
            return_value=OpenAIConversationResult(
                text=GUIDANCE,
                used=True,
                grounded=True,
                model="gpt-test",
                latency_ms=10,
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                metadata={},
            ),
        ), patch(
            "assistant_core.services.chat_processing._apply_refined_reply",
            side_effect=lambda _det, reply, **_kw: {"reply": reply},
        ):
            payload = _refine_response_with_ai_if_enabled(
                deterministic_result=SimpleNamespace(
                    assistant_profile=SimpleNamespace(use_ai=True),
                    decision=SimpleNamespace(continuity_action="", handoff_request_id=None, collection_prompt=False),
                    response_payload={"reply": DIAGNOSTIC},
                    tenant=self.tenant,
                    conversation=self.conversation,
                    user_message="São três câmaras com controle de temperatura.",
                    history=self.history,
                    dialogue_memory=None,
                    knowledge_result=None,
                    assistant_message=SimpleNamespace(pk=1),
                    chat_request=SimpleNamespace(),
                ),
                decision_service=self.service,
                knowledge_context=KB,
            )
        self.assertIn(OFFER, payload["reply"])
        lead = LeadDraft.objects.filter(conversation=self.conversation).first()
        self.assertIsNotNone(lead)
        self.assertEqual(lead.qualification_data.get(STATUS_KEY), "pending")
        self.assertFalse(lead.qualification_data.get("collection_active"))

    def test_b_deterministic_path_offers_same_as_before(self):
        reply = self.turn(NEED)
        self.assertIn(OFFER, reply.reply)
        self.assertEqual(self.lead().qualification_data.get(STATUS_KEY), "pending")
        self.assertFalse(self.lead().qualification_data.get("collection_active"))

    def test_c_diagnostic_question_blocks_offer(self):
        _, action = offer_after_guidance(
            conversation=self.conversation,
            message="Como funciona o sensor de temperatura?",
            history=[{"role": "user", "content": NEED}],
            reply=DIAGNOSTIC,
            knowledge_context=KB,
        )
        self.assertEqual(action, "")
        self.assertNotIn(OFFER, DIAGNOSTIC)

    def test_d_offer_appears_once(self):
        self.turn(NEED)
        second = self.turn("Entendi, faz sentido.")
        self.assertNotIn(OFFER, second.reply)

    def test_e_pending_acceptance_activates_collection(self):
        self.turn(NEED)
        reply = self.turn("Pode sim.")
        lead = self.lead()
        self.assertEqual(lead.qualification_data.get(STATUS_KEY), "accepted")
        self.assertTrue(lead.qualification_data.get("collection_active"))
        self.assertIn("chamar", reply.reply.lower())

    def test_f_yes_without_pending_does_not_activate_collection(self):
        self.turn("sim", knowledge="")
        lead = LeadDraft.objects.filter(conversation=self.conversation).first()
        self.assertFalse(lead and lead.qualification_data.get("collection_active"))

    def test_g_interruption_preserves_pending_without_reoffer(self):
        self.turn(NEED)
        reply = self.turn("Antes disso, isso funciona sem operador?")
        self.assertNotIn(OFFER, reply.reply)
        self.assertEqual(self.lead().qualification_data.get(STATUS_KEY), "pending")
        self.assertFalse(self.lead().qualification_data.get("collection_active"))

    def test_h_decline_does_not_repeat_immediately(self):
        self.turn(NEED)
        self.turn("Não precisa.")
        second = self.turn("Entendi.")
        self.assertNotIn(OFFER, second.reply)
        self.assertEqual(self.lead().qualification_data.get(STATUS_KEY), "declined")


@override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
class Phase52NameCompanyTests(TestCase):
    def test_i_three_word_person_name_does_not_fill_company(self):
        inferred = infer_pending_field_values("Marcelo Staging Multi", "name")
        self.assertEqual(inferred, {"name": "Marcelo Staging Multi"})
        self.assertNotIn("company", inferred)

    def test_j_introduced_name_does_not_fill_company(self):
        inferred = infer_pending_field_values("Meu nome é Ana", "name")
        self.assertEqual(inferred.get("name"), "Ana")
        self.assertNotIn("company", inferred)

    def test_k_affiliation_extracts_name_and_company(self):
        inferred = infer_pending_field_values("Sou Carlos da Empresa XYZ", "name")
        self.assertEqual(inferred.get("name"), "Carlos")
        self.assertEqual(inferred.get("company"), "Empresa XYZ")

    def test_l_explicit_company_marker(self):
        inferred = infer_pending_field_values("Minha empresa é ACME", "company")
        self.assertEqual(inferred.get("company"), "ACME")

    @override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
    def test_m_pf_without_b2b_does_not_prompt_company(self):
        tenant = Tenant.objects.create(name="PF", slug="pf-phase52")
        conversation = Conversation.objects.create(tenant=tenant, session_id="pf")
        service = LiviaDecisionService()
        history = []
        pf_need = "Meu ar-condicionado de casa apresenta problema e desliga sozinho há três dias seguidos."
        pf_kb = "[KNOWLEDGE_BASE]\nAr-condicionado residencial que desliga sozinho pode indicar falha elétrica.\n[/KNOWLEDGE_BASE]"

        def turn(text):
            nonlocal history
            result = service.generate_reply(history, text, conversation=conversation, knowledge_context=pf_kb)
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": result.reply}])
            return result

        turn(pf_need)
        turn("Pode registrar")
        turn("Ana")
        turn("11999999999")
        reply = turn("ana@example.com")
        lead = LeadDraft.objects.get(conversation=conversation)
        self.assertNotIn("empresa", reply.reply.lower())
        self.assertNotIn("company", pending_collection_fields(lead=lead))

    @override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
    def test_n_b2b_context_may_prompt_company(self):
        tenant = Tenant.objects.create(name="B2B", slug="b2b-phase52")
        conversation = Conversation.objects.create(tenant=tenant, session_id="b2b")
        service = LiviaDecisionService()
        history = []
        b2b_need = "Preciso automatizar o controle de temperatura de três câmaras na empresa."
        kb = "[KNOWLEDGE_BASE]\nControle de temperatura das câmaras exige sensores e cargas.\n[/KNOWLEDGE_BASE]"

        def turn(text):
            nonlocal history
            result = service.generate_reply(history, text, conversation=conversation, knowledge_context=kb)
            history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": result.reply}])
            return result

        turn(b2b_need)
        turn("Pode registrar")
        turn("Marcelo Silva")
        turn("11999999999")
        reply = turn("marcelo@example.com")
        lead = LeadDraft.objects.get(conversation=conversation)
        self.assertIn("continuidade", reply.reply.lower())
        self.assertEqual(pending_collection_fields(lead=lead), ["company"])


@override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
class Phase52HandoffOrderTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Handoff", slug="handoff-phase52")
        self.conversation = Conversation.objects.create(tenant=self.tenant, session_id=str(uuid.uuid4()))
        self.service = LiviaDecisionService()
        self.history: list[dict[str, str]] = []

    def turn(self, message, knowledge=""):
        result = self.service.generate_reply(
            self.history, message, conversation=self.conversation, knowledge_context=knowledge,
        )
        self.history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": result.reply}])
        return result

    def lead(self):
        return LeadDraft.objects.get(conversation=self.conversation)

    def test_o_explicit_human_without_data_asks_name_first(self):
        reply = self.turn("Gostaria que alguém da equipe entrasse em contato.")
        lowered = reply.reply.lower()
        self.assertIn("chamar", lowered)
        self.assertNotIn("telefone", lowered)
        self.assertNotIn("e-mail", lowered)
        self.assertTrue(self.lead().qualification_data.get("collection_active"))

    def test_p_after_name_asks_phone(self):
        self.turn("Gostaria que alguém da equipe entrasse em contato.")
        reply = self.turn("Marcelo")
        self.assertIn("telefone", reply.reply.lower())
        self.assertIn("retornar", reply.reply.lower())

    def test_q_after_phone_asks_email(self):
        self.turn("Gostaria que alguém da equipe entrasse em contato.")
        self.turn("Marcelo")
        reply = self.turn("11999999999")
        self.assertIn("e-mail", reply.reply.lower())
        self.assertNotIn("telefone", reply.reply.lower())

    def test_r_known_relational_name_skips_name_prompt(self):
        self.history = [
            {"role": "user", "content": "O Duno consegue limpar um galpão?"},
            {"role": "assistant", "content": "Sim, depende do piso e do fluxo."},
            {"role": "user", "content": "Meu nome é Marcelo."},
            {"role": "assistant", "content": "Prazer, Marcelo."},
        ]
        LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            name="Marcelo",
            field_sources={"name": "explicit"},
        )
        reply = self.turn("Quero falar com alguém.")
        self.assertIn("telefone", reply.reply.lower())
        self.assertNotIn("chamar", reply.reply.lower())

    def test_s_known_phone_skips_to_next_field(self):
        LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            name="Marcelo",
            phone="11999999999",
            field_sources={"name": "explicit", "phone": "explicit"},
            qualification_data={"collection_active": True},
        )
        reply = self.turn("Quero orçamento.")
        self.assertIn("e-mail", reply.reply.lower())
        self.assertNotIn("telefone", reply.reply.lower())

    def test_t_spontaneous_data_is_not_reasked(self):
        reply = self.turn(
            "Meu nome é Carlos, meu WhatsApp é 11988887777 e quero orçamento."
        )
        lowered = reply.reply.lower()
        self.assertNotIn("chamar", lowered)
        self.assertIn("e-mail", lowered)
        lead = self.lead()
        self.assertEqual(lead.name, "Carlos")
        self.assertEqual(lead.phone, "11988887777")

    def test_u_one_question_per_turn(self):
        reply = self.turn("Gostaria que alguém da equipe entrasse em contato.")
        self.assertEqual(reply.reply.count("?"), 1)
