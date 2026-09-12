"""Offline coverage for the post-guidance opt-in lifecycle."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from assistant_core.continuity_policy import (
    OFFER, STATUS_KEY, concrete_need, offer_after_guidance,
)
from assistant_core.services.livia_decision import LiviaDecisionService
from conversations.models import Conversation, HandoffRequest
from integrations.models import OutboxEvent
from leads.models import LeadDraft
from knowledge_base.rag.context_builder import KnowledgeContextResult
from tenants.models import Tenant, AssistantProfile, TenantAllowedOrigin

NEED = "Preciso automatizar o controle de temperatura de três câmaras."
GUIDANCE = "O controle de temperatura das câmaras exige avaliar sensores e cargas. O projeto depende das condições de instalação."
KB = "[KNOWLEDGE_BASE]\nConteúdo:\n" + GUIDANCE + "\n[/KNOWLEDGE_BASE]"


@override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
class ContinuityOfferTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Equipe Técnica", slug="equipe-tecnica")
        self.conversation = Conversation.objects.create(tenant=self.tenant, session_id="offer")
        self.service = LiviaDecisionService()
        self.history = []

    def offer(self, message=NEED, reply=GUIDANCE, knowledge=KB):
        reply, action = offer_after_guidance(
            conversation=self.conversation, message=message, history=self.history,
            reply=reply, knowledge_context=knowledge,
        )
        self.history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply}])
        return reply, action

    def turn(self, message, knowledge=KB):
        reply = self.service.generate_reply(
            self.history, message, conversation=self.conversation, knowledge_context=knowledge,
        )
        self.history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply.reply}])
        return reply

    def lead(self):
        return LeadDraft.objects.get(conversation=self.conversation)

    def test_informational_questions_do_not_offer(self):
        for message in ("O que é automação industrial?", "O Duno funciona com bateria?",
                        "Qual a diferença entre CLP e relé?", "Vocês trabalham com robôs?",
                        "Preciso de ajuda", "Quero saber isso", "Estou fazendo pesquisa acadêmica sobre projeto de câmaras",
                        "Preciso entender como funciona um sistema de controle de temperatura",
                        "Não preciso instalar um sistema de controle de temperatura nas câmaras",
                        "Meu manual explica o controle de temperatura para as câmaras"):
            with self.subTest(message=message):
                _, action = self.offer(message)
                self.assertEqual(action, "")
        self.assertFalse(LeadDraft.objects.filter(conversation=self.conversation).exists())

    def test_guidance_precedes_offer_and_does_not_activate_collection(self):
        reply, action = self.offer()
        self.assertEqual(action, "offered")
        self.assertEqual(reply, GUIDANCE + "\n\n" + OFFER)
        lead = self.lead()
        self.assertEqual(lead.qualification_data[STATUS_KEY], "pending")
        self.assertFalse(lead.qualification_data.get("collection_active"))
        self.assertFalse(self.conversation.is_qualified)
        self.assertFalse(HandoffRequest.objects.exists())
        self.assertFalse(OutboxEvent.objects.exists())

    def test_acceptance_uses_known_need_and_next_slot(self):
        self.offer()
        reply = self.turn("Pode sim.")
        lead = self.lead()
        self.assertEqual(lead.qualification_data[STATUS_KEY], "accepted")
        self.assertTrue(lead.qualification_data["collection_active"])
        self.assertIn("temperatura", lead.need_summary)
        self.assertEqual(lead.name, "")
        self.assertIn("nome", reply.reply.lower())
        self.assertNotIn("necessidade principal", reply.reply)
        self.assertNotIn(OFFER, reply.reply)

    def test_real_decision_path_offers_after_guidance(self):
        reply = self.turn(NEED)
        self.assertIn(OFFER, reply.reply)
        self.assertFalse(self.lead().qualification_data.get("collection_active"))

    def test_context_answer_completes_previous_need(self):
        self.history = [{"role": "user", "content": "Preciso automatizar o controle de temperatura"},
                        {"role": "assistant", "content": "Quantas câmaras?"}]
        self.assertEqual(self.offer("São três câmaras")[1], "offered")
        self.assertIn("três câmaras", self.lead().need_summary)

    @override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"], LIVIA_CHAT_RATE_LIMIT_ENABLED=False)
    @patch("assistant_core.services.chat_processing.build_knowledge_context_result")
    def test_endpoint_persists_offer_and_acceptance(self, retrieval):
        retrieval.return_value = KnowledgeContextResult(text=KB, mode="keyword", retrieval_hit=True)
        AssistantProfile.objects.create(tenant=self.tenant, name="Lívia", use_ai=False)
        TenantAllowedOrigin.objects.create(tenant=self.tenant, origin="https://example.com")
        for message in (NEED, "Pode sim."):
            request_id = str(uuid.uuid4())
            response = self.client.post(
                "/api/chat/", data=json.dumps({"tenant": self.tenant.slug, "session_id": "offer",
                    "request_id": request_id, "message": message}), content_type="application/json",
                HTTP_ORIGIN="https://example.com", HTTP_X_LIVIA_TENANT=self.tenant.slug,
                HTTP_X_LIVIA_REQUEST_ID=request_id,
            )
            self.assertEqual(response.status_code, 200, response.content)
            reply = response.json()["reply"]
            if message == NEED:
                self.assertIn(OFFER, reply)
                self.assertIn("temperatura", reply)
                self.assertFalse(self.lead().qualification_data.get("collection_active"))
            else:
                self.assertNotIn(OFFER, reply)
                self.assertIn("nome", reply.lower())
                self.assertTrue(self.lead().qualification_data["collection_active"])
            self.assertEqual(self.conversation.messages.filter(role="assistant").latest("id").content, reply)

    def test_acceptance_preserves_known_name_and_field_source(self):
        self.offer()
        lead = self.lead()
        lead.name = "Ana Silva"
        lead.field_sources = {**lead.field_sources, "name": "explicit"}
        lead.save()
        # Use a fresh conversation to avoid a cached reverse relation.
        self.conversation = Conversation.objects.get(pk=self.conversation.pk)
        reply = self.turn("Pode registrar")
        self.assertIn("telefone", reply.reply.lower())
        self.assertEqual(self.lead().name, "Ana Silva")
        self.assertEqual(self.lead().field_sources["name"], "explicit")

    def test_decline_does_not_repeat_or_qualify(self):
        self.offer()
        reply = self.turn("Não precisa.")
        self.assertEqual(reply.continuity_action, "declined")
        self.assertNotIn(OFFER, reply.reply)
        self.assertEqual(self.lead().qualification_data[STATUS_KEY], "declined")
        self.assertFalse(self.lead().qualification_data.get("collection_active"))
        _, action = self.offer()
        self.assertEqual(action, "")
        self.assertFalse(OutboxEvent.objects.exists())

    def test_yes_without_offer_does_not_activate_collection(self):
        self.turn("sim", knowledge="")
        lead = LeadDraft.objects.filter(conversation=self.conversation).first()
        self.assertFalse(lead and lead.qualification_data.get("collection_active"))

    def test_explicit_budget_does_not_need_offer(self):
        reply = self.turn("Quero orçamento.")
        self.assertTrue(self.lead().qualification_data["collection_active"])
        self.assertNotIn(OFFER, reply.reply)

    def test_explicit_human_preserves_existing_flow(self):
        reply = self.turn("Quero falar com alguém.")
        self.assertTrue(self.lead().qualification_data["collection_active"])
        self.assertNotIn(OFFER, reply.reply)

    def test_pending_offer_is_not_repeated(self):
        self.offer()
        self.assertEqual(self.offer()[1], "")

    def test_interruption_preserves_pending_offer_without_collection(self):
        self.offer()
        reply = self.turn("Antes disso, isso pode danificar a placa?")
        self.assertTrue(reply.reply.strip())
        self.assertNotIn(OFFER, reply.reply)
        self.assertEqual(self.lead().qualification_data[STATUS_KEY], "pending")
        self.assertFalse(self.lead().qualification_data.get("collection_active"))
        accepted = self.turn("Pode registrar")
        self.assertEqual(accepted.continuity_action, "accepted")

    def test_yes_to_diagnostic_question_is_not_commercial_acceptance(self):
        self.offer()
        self.history.append({"role": "assistant", "content": "O equipamento desliga?"})
        self.turn("sim")
        self.assertFalse(self.lead().qualification_data.get("collection_active"))
        self.assertEqual(self.lead().qualification_data[STATUS_KEY], "pending")

    def test_early_diagnosis_does_not_offer(self):
        reply, action = self.offer(
            "Meu ar-condicionado mostra E5.",
            "O significado depende da marca e do modelo. Qual é a marca?",
        )
        self.assertEqual(action, "")
        self.assertNotIn(OFFER, reply)

    def test_no_evidence_does_not_offer(self):
        self.assertEqual(self.offer(knowledge="")[1], "")

    def test_safety_guidance_precedes_offer(self):
        need = "Meu quadro apresenta falha e fumaça no controle de temperatura das câmaras."
        safety = "Evite abrir o quadro energizado. Procure um profissional habilitado para avaliar a falha no controle de temperatura."
        reply, action = self.offer(need, safety, safety)
        self.assertEqual(action, "offered")
        self.assertTrue(reply.startswith(safety))
        self.assertTrue(reply.endswith(OFFER))

    def test_tenant_state_and_evidence_are_isolated(self):
        self.offer()
        other = Tenant.objects.create(name="Outra Equipe", slug="outra-equipe")
        self.conversation = Conversation.objects.create(tenant=other, session_id="offer")
        self.history = []
        self.turn("sim", knowledge="")
        lead = LeadDraft.objects.filter(conversation=self.conversation).first()
        self.assertFalse(lead and lead.qualification_data.get("collection_active"))
        self.assertEqual(self.offer(knowledge="Bancadas de granito para cozinhas.")[1], "")
        self.assertEqual(self.offer()[1], "offered")
        self.assertEqual(self.lead().tenant_id, other.pk)

    def test_new_concrete_need_allows_new_offer_after_decline(self):
        self.offer()
        self.turn("Não precisa")
        self.assertTrue(concrete_need("Outro projeto: " + NEED))
        self.assertEqual(self.offer("Outro projeto: " + NEED)[1], "offered")

    @patch("assistant_core.services.chat_processing._can_refine_with_ai", return_value=True)
    @patch("assistant_core.services.openai_grounded_conversation.OpenAIGroundedConversationService.generate")
    def test_ai_cannot_rewrite_operational_offer_turns(self, generate, can_refine):
        from assistant_core.services.chat_processing import _refine_response_with_ai_if_enabled
        for action in ("offered", "accepted", "declined"):
            payload = {"reply": "Resposta operacional"}
            result = _refine_response_with_ai_if_enabled(
                deterministic_result=SimpleNamespace(
                    assistant_profile=object(), decision=SimpleNamespace(continuity_action=action),
                    response_payload=payload,
                ), decision_service=self.service,
            )
            self.assertEqual(result, payload)
        generate.assert_not_called()

    def test_all_short_acceptances_require_pending_offer(self):
        for message in ("sim", "pode", "pode sim", "ok", "claro", "quero", "vamos", "pode registrar", "pode encaminhar"):
            with self.subTest(message=message):
                self.conversation = Conversation.objects.create(tenant=self.tenant, session_id=str(uuid.uuid4()))
                self.history = []
                self.offer()
                self.assertEqual(self.turn(message).continuity_action, "accepted")
                self.assertTrue(self.lead().qualification_data["collection_active"])

    def test_all_short_declines_leave_collection_inactive(self):
        for message in ("não", "não precisa", "agora não", "só queria saber isso", "obrigado", "deixa para depois"):
            with self.subTest(message=message):
                self.conversation = Conversation.objects.create(tenant=self.tenant, session_id=str(uuid.uuid4()))
                self.history = []
                self.offer()
                self.assertEqual(self.turn(message).continuity_action, "declined")
                self.assertFalse(self.lead().qualification_data.get("collection_active"))
