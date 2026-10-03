"""Cobertura RAG SCB para tópicos de marketing digital, climatização e Connect Bot."""

from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings

from assistant_core.discovery import analyze_message
from assistant_core.services import LiviaDecisionService
from assistant_core.services.conversation_decision import ConversationAction
from assistant_core.services.livia_decision import LiviaReply
from assistant_core.services.openai_grounded_conversation import OpenAIGroundedConversationService
from conversations.models import Conversation
from integrations.models import OutboxEvent
from integrations.openai.client import OpenAIChatResult
from knowledge_base.rag.context_builder import build_knowledge_context_result
from leads.models import LeadDraft
from tenants.models import AssistantProfile, Tenant


class _FakeAIClient:
    def __init__(self, text: str):
        self.calls: list[list[dict[str, str]]] = []
        self._result = OpenAIChatResult(text=text, success=True, dry_run=False, model="gpt-4.1-mini")

    def create_chat_completion(self, *, messages):
        self.calls.append(messages)
        return self._result


@override_settings(
    LIVIA_RAG_ENABLED=True,
    LIVIA_RAG_DRY_RUN=False,
    LIVIA_RAG_EMBEDDING_PROVIDER="fake",
    LIVIA_RAG_EMBEDDING_MODEL="fake-embed-v1",
    LIVIA_ALLOW_FAKE_EMBEDDINGS=True,
    LIVIA_RAG_MIN_SIMILARITY_SCORE=0.20,
    LIVIA_RAG_MAX_RETRIEVED_CHUNKS=4,
    LIVIA_RAG_MAX_CONTEXT_CHARS=2500,
    LIVIA_AI_ENABLED=True,
    LIVIA_AI_DRY_RUN=False,
    LIVIA_OPENAI_API_KEY="test-key-local",
)
class SmartControlRagCorpusTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug=f"scb-rag-{uuid.uuid4().hex[:6]}",
            domain="smartcontrolbrasil.example",
        )
        cls.profile = AssistantProfile.objects.create(
            tenant=cls.tenant,
            name="Lívia",
            business_name="Smart Control Brasil",
            business_domain="automação e soluções digitais",
            use_ai=True,
            is_active=True,
            notification_email="comercial@smartcontrolbrasil.com.br",
        )
        fixtures_dir = Path(settings.BASE_DIR) / "knowledge_fixtures" / "smart-control-brasil"
        call_command(
            "import_tenant_knowledge",
            tenant=cls.tenant.slug,
            source=str(fixtures_dir),
            source_type="official_fixture",
            replace=True,
            verbosity=0,
        )
        call_command("reindex_tenant_knowledge", tenant=cls.tenant.slug, verbosity=0)

    def _retrieve(self, conversation: Conversation, message: str) -> str:
        discovery = analyze_message(message)
        result = build_knowledge_context_result(
            self.tenant,
            message,
            service_area=discovery.service_area,
            limit=4,
            conversation=conversation,
        )
        return result.text

    def test_retrieval_topics_have_grounded_content(self):
        cases = [
            ("vocês fazem tráfego pago?", ("tráfego pago", "mídia paga")),
            ("vocês trabalham com SEO?", ("seo", "documento dedicado")),
            ("o que vocês fazem para presença digital no Google e website?", ("presença digital", "websites", "sistemas web")),
            ("vocês trabalham com ar-condicionado?", ("ar-condicionado", "refrigeração comercial")),
            ("me fale sobre o Connect Bot", ("connect bot", "hostbot")),
        ]
        for message, expected_tokens in cases:
            with self.subTest(message=message):
                conversation = Conversation.objects.create(
                    tenant=self.tenant,
                    session_id=f"retrieval-{uuid.uuid4().hex[:8]}",
                )
                ctx = self._retrieve(conversation, message).lower()
                for token in expected_tokens:
                    self.assertIn(token, ctx)

    def test_mitsubishi_and_climatization_stay_separate_in_retrieval(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="retrieval-separation")
        mitsubishi = self._retrieve(conversation, "vocês trabalham com Mitsubishi Electric?").lower()
        self.assertIn("automação industrial", mitsubishi)
        self.assertIn("mitsubishi electric", mitsubishi)

        ar_condicionado = self._retrieve(conversation, "vocês instalam ar-condicionado?").lower()
        self.assertIn("refrigeração comercial", ar_condicionado)
        self.assertIn("ar-condicionado", ar_condicionado)

    def test_llm_prompt_receives_retrieved_chunks_for_new_topics(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="llm-grounded-topics")
        ai_client = _FakeAIClient("Resposta grounded.")
        service = OpenAIGroundedConversationService(ai_client=ai_client)
        message = "me fale sobre o Connect Bot"
        knowledge = self._retrieve(conversation, message)

        result = service.generate(
            tenant=self.tenant,
            assistant_profile=self.profile,
            message=message,
            conversation=conversation,
            discovery=analyze_message(message),
            decision=LiviaReply(intent="technical_question", reply="fallback"),
            knowledge_context=knowledge,
            history=[],
        )

        self.assertTrue(result.used)
        prompt_blob = "\n".join(chunk["content"] for chunk in ai_client.calls[0])
        self.assertIn("connect bot", prompt_blob.lower())
        self.assertIn("hostbot", prompt_blob.lower())
        self.assertIn("BASE DE CONHECIMENTO", prompt_blob)

    def test_consultative_then_budget_flow_keeps_commercial_actions_with_rag(self):
        service = LiviaDecisionService()
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="serralheria-rag-flow")
        history: list[dict[str, str]] = []
        pre_budget = [
            "me fale sobre tráfego pago",
            "quero aumentar meu número de clientes",
            "é uma serralheria",
        ]
        for msg in pre_budget:
            ctx = self._retrieve(conversation, msg)
            decision = service.generate_reply(history, msg, conversation=conversation, knowledge_context=ctx)
            history.extend([{"role": "user", "content": msg}, {"role": "assistant", "content": decision.reply}])

        actions: list[ConversationAction] = []
        for msg in ("gostaria de um orçamento", "Marcelo", "11962196100", "marcelo@example.com", "Serralheria Marcelo"):
            ctx = self._retrieve(conversation, msg)
            decision = service.generate_reply(history, msg, conversation=conversation, knowledge_context=ctx)
            history.extend([{"role": "user", "content": msg}, {"role": "assistant", "content": decision.reply}])
            actions.append(decision.structured_decision.action)

        self.assertEqual(actions[0], ConversationAction.ASK_NAME)
        self.assertEqual(actions[1], ConversationAction.ASK_PHONE)
        self.assertIn(actions[2], {ConversationAction.ASK_EMAIL, ConversationAction.ASK_COMPANY})
        self.assertEqual(actions[-1], ConversationAction.COMPLETE_HANDOFF)
        lead = LeadDraft.objects.get(conversation=conversation)
        self.assertEqual(lead.status, LeadDraft.Status.QUALIFIED)
        self.assertEqual(
            OutboxEvent.objects.filter(
                event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
                aggregate_id=str(lead.pk),
            ).count(),
            1,
        )
