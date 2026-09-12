"""Fase 5.2.1 — arbitragem explícita de perguntas."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from assistant_core.continuity_policy import OFFER, STATUS_KEY, offer_after_guidance
from assistant_core.relational_collection import build_relational_name_prompt
from assistant_core.services.chat_processing import _refine_response_with_ai_if_enabled
from assistant_core.services.livia_decision import LiviaDecisionService, LiviaReply
from assistant_core.services.openai_grounded_conversation import OpenAIConversationResult
from conversations.models import Conversation
from leads.models import LeadDraft
from tenants.models import Tenant

NEED = "Preciso automatizar o controle de temperatura de três câmaras frigoríficas."
GUIDANCE = (
    "Com sensores, CLP e IHM é possível monitorar as câmaras, registrar histórico "
    "e gerar alarmes locais e remotos."
)
GENERIC_QUESTION = GUIDANCE + " Posso ajudar a montar essa solução?"
DIAGNOSTIC_QUESTION = GUIDANCE + " Qual é a tensão da máquina?"
KB = "[KNOWLEDGE_BASE]\nConteúdo:\n" + GUIDANCE + " Sensores, CLP, IHM e alarmes para câmaras frigoríficas.\n[/KNOWLEDGE_BASE]"


def question_count(text: str) -> int:
    return str(text or "").count("?")


@override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
class QuestionArbitrationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Equipe Técnica", slug="question-arbitration")
        self.conversation = Conversation.objects.create(tenant=self.tenant, session_id="arbitration")
        self.service = LiviaDecisionService()
        self.history: list[dict[str, str]] = []

    def turn(self, message: str, knowledge: str = KB):
        result = self.service.generate_reply(
            self.history,
            message,
            conversation=self.conversation,
            knowledge_context=knowledge,
        )
        self.history.extend([
            {"role": "user", "content": message},
            {"role": "assistant", "content": result.reply},
        ])
        return result

    def lead(self):
        return LeadDraft.objects.get(conversation=self.conversation)

    def test_a_diagnostic_question_blocks_continuity_and_relational_name(self):
        reply, action = offer_after_guidance(
            conversation=self.conversation,
            message=NEED,
            history=[],
            reply=DIAGNOSTIC_QUESTION,
            knowledge_context=KB,
        )
        self.assertEqual(action, "")
        self.assertNotIn(OFFER, reply)
        self.assertNotIn("chamar", reply.lower())
        self.assertEqual(question_count(reply), 1)

    def test_b_generic_question_does_not_block_continuity(self):
        reply, action = offer_after_guidance(
            conversation=self.conversation,
            message=NEED,
            history=[],
            reply=GENERIC_QUESTION,
            knowledge_context=KB,
        )
        self.assertEqual(action, "offered")
        self.assertIn(OFFER, reply)
        self.assertNotIn("montar essa solução?", reply)
        self.assertNotIn("chamar", reply.lower())
        self.assertEqual(question_count(reply), 0)

    def test_c_no_question_gets_continuity_when_eligible(self):
        reply, action = offer_after_guidance(
            conversation=self.conversation,
            message=NEED,
            history=[],
            reply=GUIDANCE,
            knowledge_context=KB,
        )
        self.assertEqual(action, "offered")
        self.assertTrue(reply.endswith(OFFER))

    def test_d_e_pending_acceptance_starts_collection_and_asks_name(self):
        first = self.turn(NEED)
        self.assertIn(OFFER, first.reply)
        accepted = self.turn("Pode sim.")
        lead = self.lead()
        self.assertEqual(lead.qualification_data.get(STATUS_KEY), "accepted")
        self.assertTrue(lead.qualification_data.get("collection_active"))
        self.assertIn("chamar", accepted.reply.lower())
        self.assertEqual(question_count(accepted.reply), 1)

    def test_f_diagnostic_question_does_not_mark_relational_name(self):
        lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation)
        history = [
            {"role": "user", "content": "O Duno consegue limpar um galpão?"},
            {"role": "assistant", "content": "Ele pode apoiar limpeza de áreas amplas."},
            {"role": "user", "content": "E consegue trabalhar várias horas?"},
            {"role": "assistant", "content": "A autonomia depende da configuração."},
        ]
        reply = self.service._finalize_ai_response(
            decision=LiviaReply(intent="technical_question", reply=DIAGNOSTIC_QUESTION),
            conversation=self.conversation,
            assistant_profile=None,
            discovery=SimpleNamespace(),
            current_message="A máquina está em 220V e desliga às vezes. Qual é a tensão ideal?",
            history=history,
            knowledge_context=KB,
        )
        lead.refresh_from_db()
        lead = self.lead()
        self.assertIn("?", reply.reply)
        self.assertFalse(lead.qualification_data.get("relational_name_asked"))

    def test_g_continuity_offer_does_not_mark_relational_name(self):
        reply = self.turn(NEED)
        lead = self.lead()
        self.assertIn(OFFER, reply.reply)
        self.assertFalse(lead.qualification_data.get("relational_name_asked"))

    def test_h_relational_name_still_works_when_no_higher_priority_action(self):
        self.turn("O Duno consegue limpar um galpão?")
        self.turn("E consegue trabalhar várias horas?")
        reply = self.turn("Como funciona o mapeamento?")
        lead = self.lead()
        self.assertIn("chamar", reply.reply.lower())
        self.assertTrue(lead.qualification_data.get("relational_name_asked"))
        self.assertEqual(question_count(reply.reply), 1)

    def test_i_final_reply_has_at_most_one_question_in_real_scenario(self):
        self.turn("Estou estudando automatizar o controle de temperatura de três câmaras frigoríficas. Como vocês fariam isso?")
        self.turn("Hoje não temos CLP nem supervisório. Quero acompanhar a temperatura das três câmaras, receber alarmes e visualizar tudo em um painel.")
        third = self.turn(
            "Sim. As três câmaras trabalham a -18 °C. Quero sensor de temperatura em cada uma, "
            "histórico das medições, alarmes no painel e também alertas remotos. A IHM pode ficar na sala técnica."
        )
        lead = self.lead()
        self.assertIn("sensores", third.reply.lower())
        self.assertIn(OFFER, third.reply)
        self.assertLessEqual(question_count(third.reply), 1)
        self.assertFalse(lead.qualification_data.get("relational_name_asked"))
        self.assertEqual(lead.qualification_data.get(STATUS_KEY), "pending")
        self.assertFalse(lead.qualification_data.get("collection_active"))
        accepted = self.turn("Pode sim.")
        self.assertIn("chamar", accepted.reply.lower())
        self.assertTrue(self.lead().qualification_data.get("collection_active"))

    def test_i2_accumulated_need_policy_is_multi_domain(self):
        scenarios = (
            {
                "slug": "winder",
                "messages": [
                    "Tenho uma bobinadora manual.",
                    "Quero aumentar produtividade e padronizar qualidade.",
                ],
                "reply": "A automação pode reduzir variação operacional e organizar repetibilidade no processo.",
                "kb": "[KNOWLEDGE_BASE]\nBobinadora manual com produtividade e qualidade padronizada por solução técnica.\n[/KNOWLEDGE_BASE]",
            },
            {
                "slug": "cleaning-robot",
                "messages": [
                    "Preciso de um robô para um galpão.",
                    "São 3000 m² e piso de concreto.",
                ],
                "reply": "Para esse ambiente, a análise considera área útil, piso, circulação e rotina operacional.",
                "kb": "[KNOWLEDGE_BASE]\nRobô para galpão de 3000 m² com piso de concreto depende de área útil e rotina.\n[/KNOWLEDGE_BASE]",
            },
            {
                "slug": "website",
                "messages": [
                    "Quero modernizar meu site.",
                    "Preciso gerar mais contatos comerciais.",
                ],
                "reply": "Um site comercial pode organizar proposta, páginas de serviço e conversões para contato.",
                "kb": "[KNOWLEDGE_BASE]\nSite moderno para gerar contatos comerciais com páginas de serviço e conversão.\n[/KNOWLEDGE_BASE]",
            },
            {
                "slug": "chambers",
                "messages": [
                    "Tenho três câmaras.",
                    "Quero histórico, alarmes e monitoramento.",
                ],
                "reply": "A solução pode organizar monitoramento, histórico operacional e alarmes para acompanhamento.",
                "kb": "[KNOWLEDGE_BASE]\nTrês câmaras com histórico, alarmes e monitoramento exigem solução operacional adequada.\n[/KNOWLEDGE_BASE]",
            },
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario["slug"]):
                conversation = Conversation.objects.create(tenant=self.tenant, session_id=scenario["slug"])
                history = [{"role": "user", "content": scenario["messages"][0]}]
                reply, action = offer_after_guidance(
                    conversation=conversation,
                    message=scenario["messages"][1],
                    history=history,
                    reply=scenario["reply"] + " Posso ajudar a montar essa solução?",
                    knowledge_context=scenario["kb"],
                )
                lead = LeadDraft.objects.get(conversation=conversation)
                self.assertEqual(action, "offered")
                self.assertIn(OFFER, reply)
                self.assertLessEqual(question_count(reply), 1)
                self.assertEqual(lead.qualification_data.get(STATUS_KEY), "pending")
                self.assertFalse(lead.qualification_data.get("collection_active"))

    def test_j_decline_continuity_does_not_activate_collection(self):
        self.turn(NEED)
        declined = self.turn("Não precisa.")
        lead = self.lead()
        self.assertEqual(declined.continuity_action, "declined")
        self.assertFalse(lead.qualification_data.get("collection_active"))

    def test_k_pending_interruption_answers_technical_and_does_not_ask_name(self):
        self.turn(NEED)
        interrupted = self.turn("Antes disso, isso funciona sem operador?")
        lead = self.lead()
        self.assertNotIn("chamar", interrupted.reply.lower())
        self.assertEqual(lead.qualification_data.get(STATUS_KEY), "pending")
        self.assertFalse(lead.qualification_data.get("collection_active"))
        self.assertLessEqual(question_count(interrupted.reply), 1)


@override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
class QuestionArbitrationOpenAIPathTests(TestCase):
    def test_l_openai_refinement_uses_same_priority_and_clears_preempted_relational_flag(self):
        tenant = Tenant.objects.create(name="Equipe Técnica", slug="question-arbitration-ai")
        conversation = Conversation.objects.create(tenant=tenant, session_id="ai")
        lead = LeadDraft.objects.create(
            tenant=tenant,
            conversation=conversation,
            qualification_data={"relational_name_asked": True},
        )
        prompt = build_relational_name_prompt()
        deterministic = SimpleNamespace(
            assistant_profile=SimpleNamespace(use_ai=True),
            decision=SimpleNamespace(continuity_action="", handoff_request_id=None, collection_prompt=False),
            response_payload={"reply": f"{GUIDANCE} {prompt}"},
            tenant=tenant,
            conversation=conversation,
            user_message="São três câmaras com sensores e alarmes.",
            history=[{"role": "user", "content": NEED}],
            dialogue_memory=None,
            knowledge_result=None,
            assistant_message=SimpleNamespace(pk=1),
            chat_request=SimpleNamespace(),
        )
        with patch("assistant_core.services.chat_processing._can_refine_with_ai", return_value=True), patch(
            "assistant_core.services.openai_grounded_conversation.OpenAIGroundedConversationService.generate",
            return_value=OpenAIConversationResult(
                text=GENERIC_QUESTION,
                used=True,
                grounded=True,
                model="gpt-test",
                latency_ms=1,
                metadata={},
            ),
        ), patch(
            "assistant_core.services.chat_processing._apply_refined_reply",
            side_effect=lambda _det, reply, **_kw: {"reply": reply},
        ):
            payload = _refine_response_with_ai_if_enabled(
                deterministic_result=deterministic,
                decision_service=LiviaDecisionService(),
                knowledge_context=KB,
            )
        lead.refresh_from_db()
        self.assertIn(OFFER, payload["reply"])
        self.assertNotIn("montar essa solução?", payload["reply"])
        self.assertNotIn("chamar", payload["reply"].lower())
        self.assertFalse(lead.qualification_data.get("relational_name_asked"))
