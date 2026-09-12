"""Testes da camada OpenAI como motor conversacional principal (mockado)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from assistant_core.consultative_policy import mark_collection_active
from assistant_core.dialogue_memory import DialogueMemory, update_dialogue_memory_from_turn
from assistant_core.discovery import analyze_message
from assistant_core.services.chat_processing import (
    _DeterministicChatResult,
    _refine_response_with_ai_if_enabled,
)
from assistant_core.services.livia_decision import LiviaDecisionService, LiviaReply
from assistant_core.services.openai_grounded_conversation import (
    OpenAIGroundedConversationService,
    OpenAIConversationResult,
)
from conversations.models import Conversation, Message
from integrations.openai.client import OpenAIChatResult
from knowledge_base.models import KnowledgeDocument, TenantRagConfiguration
from knowledge_base.rag.context_builder import build_knowledge_context_result
from knowledge_base.rag.embeddings import FakeEmbeddingProvider, load_embedding_config
from knowledge_base.rag.indexing import run_index_for_tenant
from knowledge_base.rag.sync import run_chunk_build_for_tenant
from knowledge_base.services.manual_rag import sync_manual_knowledge_document_to_rag
from leads.models import LeadDraft
from tenants.models import AssistantProfile, Tenant


HYGIBOT_KB = """
# HygiBot / Dune Bot — robô de limpeza autônoma

Nome oficial: HygiBot / Dune Bot
Categoria: limpeza profissional

## Aplicação
Apoiar rotinas de limpeza em grandes áreas, com modos de lavar, varrer, aspirar e passar pano.

## Ambientes
- shoppings, indústrias, hospitais, grandes áreas.

## Limites
A escolha depende de tipo de piso, fluxo de pessoas, horários e obstáculos.
"""

LIRO_KB = """
# LIRO / Little Bot — robô educacional interativo

Nome oficial: LIRO / Little Bot
Categoria: robótica educacional

## Aplicação
Robô interativo para aproximar crianças e jovens da tecnologia por meio de experiências educacionais.
"""

CLEANMASTER_Z9_KB = """
# CleanMaster Z9 — robô de limpeza industrial fictício

Nome oficial: CleanMaster Z9
Categoria: limpeza profissional de pisos industriais

## Aplicação
Limpeza autônoma de galpões com piso de concreto, incluindo varredura e lavagem.

## Diferencial
Operação noturna com detecção de circulação de pessoas.
"""


class FakeAIClient:
    def __init__(self, result: OpenAIChatResult | None = None, exc: Exception | None = None):
        self.result = result or OpenAIChatResult(text="", success=False, dry_run=False)
        self.exc = exc
        self.calls: list[list[dict[str, str]]] = []

    def create_chat_completion(self, *, messages):
        self.calls.append(messages)
        if self.exc:
            raise self.exc
        return self.result


@override_settings(
    LIVIA_AI_ENABLED=True,
    LIVIA_AI_DRY_RUN=False,
    LIVIA_OPENAI_API_KEY="test-key-local",
    LIVIA_RAG_ENABLED=True,
    LIVIA_RAG_DRY_RUN=False,
    LIVIA_RAG_INDEXING_ENABLED=True,
    LIVIA_RAG_EMBEDDING_PROVIDER="fake",
    LIVIA_RAG_EMBEDDING_MODEL="fake-embed-v1",
    LIVIA_RAG_EMBEDDING_BATCH_SIZE=4,
    LIVIA_RAG_EMBEDDING_TIMEOUT_SECONDS=5,
    LIVIA_RAG_EMBEDDING_MAX_RETRIES=0,
    LIVIA_RAG_EMBEDDING_RETRY_BACKOFF_SECONDS=0,
    LIVIA_RAG_MIN_SIMILARITY_SCORE=0.10,
    LIVIA_RAG_MAX_RETRIEVED_CHUNKS=4,
    LIVIA_RAG_MAX_CONTEXT_CHARS=2000,
    LIVIA_RAG_MAX_CHUNKS_PER_MANIFEST=2,
)
class OpenAIConversationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug=f"scb-openai-{uuid.uuid4().hex[:6]}",
            domain="https://scb-openai.example",
        )
        self.profile = AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            business_name="Smart Control Brasil",
            business_domain="robótica de serviço",
            use_ai=True,
            is_active=True,
        )
        self.conversation = Conversation.objects.create(tenant=self.tenant, session_id="openai-conv")
        self.provider = FakeEmbeddingProvider()
        self.embedding_config = load_embedding_config()
        self._ingest("hygibot", "HygiBot", HYGIBOT_KB)
        self._ingest("liro", "LIRO", LIRO_KB)

    def _ingest(self, slug: str, title: str, content: str):
        document = KnowledgeDocument.objects.create(
            tenant=self.tenant,
            title=title,
            slug=f"{slug}-{uuid.uuid4().hex[:6]}",
            content=content,
            status=KnowledgeDocument.Status.ACTIVE,
        )
        sync_manual_knowledge_document_to_rag(document=document)
        configuration = TenantRagConfiguration.objects.get(tenant=self.tenant)
        configuration.retrieval_enabled = True
        configuration.save(update_fields=["retrieval_enabled", "updated_at"])
        run_chunk_build_for_tenant(configuration=configuration)
        run_index_for_tenant(
            configuration=configuration,
            provider=self.provider,
            config=self.embedding_config,
            run_id=f"idx-{self.tenant.slug}-{document.pk}",
        )

    def _service_with_client(self, text: str) -> tuple[OpenAIGroundedConversationService, FakeAIClient]:
        client = FakeAIClient(
            OpenAIChatResult(text=text, success=True, dry_run=False, model="gpt-4.1-mini", total_tokens=42)
        )
        return OpenAIGroundedConversationService(ai_client=client), client

    def _prompt_blob(self, client: FakeAIClient) -> str:
        return "\n".join(m["content"] for m in client.calls[0])

    def test_scenario1_cleaning_robot_hygibot_context(self):
        message = "preciso de um robo de limpeza"
        memory = update_dialogue_memory_from_turn(
            memory=DialogueMemory(),
            current_message=message,
            history=[],
            tenant=self.tenant,
        )
        knowledge = build_knowledge_context_result(
            self.tenant,
            message,
            limit=4,
            conversation=self.conversation,
            active_domain=memory.active_domain,
            active_entity=memory.active_entity,
            active_application=memory.active_application,
            active_subject=memory.active_knowledge_subject,
        )
        service, client = self._service_with_client(
            "O HygiBot apoia limpeza de grandes áreas com varredura, aspiração e lavagem."
        )
        discovery = analyze_message(message)
        decision = LiviaReply(intent="commercial_interest", reply="fallback determinístico")
        result = service.generate(
            tenant=self.tenant,
            assistant_profile=self.profile,
            message=message,
            conversation=self.conversation,
            discovery=discovery,
            decision=decision,
            knowledge_context=knowledge.text,
            dialogue_memory=memory,
            knowledge_result=knowledge,
        )
        self.assertTrue(result.used)
        prompt = self._prompt_blob(client)
        self.assertIn("HygiBot", prompt)
        self.assertIn(knowledge.text, prompt)
        self.assertNotIn("LIRO", knowledge.text)
        self.assertNotIn("educacional", knowledge.text.lower())

    def test_scenario2_short_followup_with_history_and_memory(self):
        history = [
            {"role": "user", "content": "preciso de um robo de limpeza"},
            {"role": "assistant", "content": "O HygiBot é voltado à limpeza de grandes áreas."},
        ]
        message = "um galpão"
        memory = DialogueMemory(active_entity="HygiBot", active_application="cleaning_robotics", active_domain="robotics")
        service, client = self._service_with_client(
            "Para um galpão, o HygiBot pode ser uma boa opção. Qual é o tamanho aproximado e o tipo de piso?"
        )
        discovery = analyze_message(message)
        result = service.generate(
            tenant=self.tenant,
            assistant_profile=self.profile,
            message=message,
            conversation=self.conversation,
            discovery=discovery,
            decision=LiviaReply(intent="discovery", reply="fallback"),
            knowledge_context="",
            history=history,
            dialogue_memory=memory,
        )
        self.assertTrue(result.used)
        prompt = self._prompt_blob(client)
        self.assertIn("preciso de um robo de limpeza", prompt)
        self.assertIn("HygiBot", prompt)
        self.assertIn("cleaning_robotics", prompt)

    def test_scenario3_pronoun_question_with_active_subject(self):
        memory = DialogueMemory(
            active_entity="HygiBot",
            active_application="cleaning_robotics",
            active_domain="robotics",
            active_knowledge_subject={"canonical_name": "HygiBot / Dune Bot", "source_document_ids": [1]},
        )
        knowledge = build_knowledge_context_result(
            self.tenant,
            "circulação de pessoas",
            limit=4,
            conversation=self.conversation,
            active_subject=memory.active_knowledge_subject,
            active_entity=memory.active_entity,
            active_application=memory.active_application,
        )
        message = "ele consegue trabalhar com pessoas circulando?"
        service, client = self._service_with_client(
            "Sim, a escolha depende do fluxo de pessoas, horários e layout — isso precisa ser avaliado no contexto."
        )
        result = service.generate(
            tenant=self.tenant,
            assistant_profile=self.profile,
            message=message,
            conversation=self.conversation,
            discovery=analyze_message(message),
            decision=LiviaReply(intent="informational", reply="fallback"),
            knowledge_context=knowledge.text,
            dialogue_memory=memory,
            knowledge_result=knowledge,
        )
        self.assertTrue(result.used)
        prompt = self._prompt_blob(client)
        self.assertIn("active_knowledge_subject", prompt)
        self.assertIn("HygiBot", prompt)
        self.assertIn(message, prompt)

    def test_scenario4_quote_triggers_collection_active_in_prompt(self):
        lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation)
        mark_collection_active(lead, reason="explicit_quote")
        lead.refresh_from_db()
        self.assertTrue((lead.qualification_data or {}).get("collection_active"))

        service, client = self._service_with_client(
            "Perfeito. Para preparar o orçamento, qual é o seu nome ou o nome da empresa?"
        )
        result = service.generate(
            tenant=self.tenant,
            assistant_profile=self.profile,
            message="quero um orçamento",
            conversation=self.conversation,
            discovery=analyze_message("quero um orçamento"),
            decision=LiviaReply(intent="quote_request", reply="Ótimo. Qual é o seu nome?"),
            knowledge_context="",
            dialogue_memory=DialogueMemory(),
        )
        self.assertTrue(result.used)
        prompt = self._prompt_blob(client)
        self.assertIn("collection_active: True", prompt)
        self.assertIn("campos comerciais permitidos: name, phone, email, need_summary", prompt)

    def test_scenario5_openai_timeout_keeps_deterministic_reply(self):
        from conversations.models import ChatRequest

        assistant_message = Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.ASSISTANT,
            content="Resposta determinística segura",
        )
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            session_id=self.conversation.session_id,
            request_id=uuid.uuid4(),
            request_fingerprint="d" * 64,
            status=ChatRequest.Status.COMPLETED,
            response_payload={"reply": "Resposta determinística segura", "intent": "discovery"},
            response_status_code=200,
        )
        deterministic_result = _DeterministicChatResult(
            chat_request=chat_request,
            tenant=self.tenant,
            conversation=self.conversation,
            assistant_profile=self.profile,
            history=[],
            user_message="preciso de um robo de limpeza",
            decision=LiviaReply(intent="discovery", reply="Resposta determinística segura"),
            assistant_message=assistant_message,
            response_payload={"reply": "Resposta determinística segura", "intent": "discovery", "observability": {}},
        )

        with patch("assistant_core.services.chat_processing._can_refine_with_ai", return_value=True), patch(
            "assistant_core.services.openai_grounded_conversation.OpenAIGroundedConversationService.generate",
            return_value=OpenAIConversationResult(
                status="failed",
                skip_reason="Timeout",
                error_type="Timeout",
            ),
        ):
            payload = _refine_response_with_ai_if_enabled(
                deterministic_result=deterministic_result,
                decision_service=LiviaDecisionService(),
            )

        self.assertEqual(payload["reply"], "Resposta determinística segura")
        self.assertTrue(payload["observability"].get("ai_fallback_used"))
        assistant_message.refresh_from_db()
        self.assertEqual(assistant_message.content, "Resposta determinística segura")
        chat_request.refresh_from_db()
        self.assertEqual(chat_request.status, ChatRequest.Status.COMPLETED)

    def test_generalization_fictitious_product_via_rag_only(self):
        self._ingest("cleanmaster_z9", "CleanMaster Z9", CLEANMASTER_Z9_KB)
        message = "vocês têm o CleanMaster Z9?"
        memory = update_dialogue_memory_from_turn(
            memory=DialogueMemory(),
            current_message=message,
            history=[],
            tenant=self.tenant,
        )
        knowledge = build_knowledge_context_result(
            self.tenant,
            message,
            limit=4,
            conversation=self.conversation,
            active_subject=memory.active_knowledge_subject,
            active_entity=memory.active_entity,
        )
        self.assertIn("CleanMaster Z9", knowledge.text)
        service, client = self._service_with_client(
            "Sim, o CleanMaster Z9 é voltado à limpeza autônoma de galpões com piso de concreto."
        )
        result = service.generate(
            tenant=self.tenant,
            assistant_profile=self.profile,
            message=message,
            conversation=self.conversation,
            discovery=analyze_message(message),
            decision=LiviaReply(intent="informational", reply="fallback genérico"),
            knowledge_context=knowledge.text,
            dialogue_memory=memory,
            knowledge_result=knowledge,
        )
        self.assertTrue(result.used)
        self.assertIn("CleanMaster Z9", result.text)
        self.assertIn("CleanMaster Z9", self._prompt_blob(client))

    @override_settings(LIVIA_AI_ENABLED=False)
    def test_gate_blocks_when_ai_disabled(self):
        from assistant_core.services.ai_feature_gates import is_openai_conversation_allowed

        self.assertFalse(is_openai_conversation_allowed(assistant_profile=self.profile))

    def test_gate_requires_use_ai_on_profile(self):
        from assistant_core.services.ai_feature_gates import is_openai_conversation_allowed

        profile = SimpleNamespace(use_ai=False)
        self.assertFalse(is_openai_conversation_allowed(assistant_profile=profile))


class DualModeConversationTests(TestCase):
    """Garante paridade de estado entre path determinístico (AI off) e OpenAI mockada (AI on)."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug=f"scb-dual-{uuid.uuid4().hex[:6]}",
            domain="https://scb-dual.example",
        )
        self.profile = AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            business_name="Smart Control Brasil",
            business_domain="sites e lojas virtuais",
            use_ai=True,
            is_active=True,
        )
        self.message = "gostaria de uma loja virtual"
        self.history = [
            {"role": "user", "content": "preciso de um site"},
            {"role": "assistant", "content": "Claro. Qual é o objetivo principal do site?"},
        ]

    def _deterministic_snapshot(self, *, session_id: str) -> dict:
        from assistant_core.services.chat_processing import process_chat_request
        from assistant_core.services.chat_idempotency import build_request_fingerprint
        from conversations.models import ChatRequest

        conversation = Conversation.objects.create(tenant=self.tenant, session_id=session_id)
        request_id = uuid.uuid4()
        fingerprint = build_request_fingerprint(
            tenant_slug=self.tenant.slug,
            session_id=session_id,
            request_id=request_id,
            message=self.message,
        )
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            session_id=session_id,
            request_id=request_id,
            request_fingerprint=fingerprint,
            status=ChatRequest.Status.PROCESSING,
        )
        Message.objects.create(conversation=conversation, role=Message.Role.USER, content="preciso de um site")
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="Claro. Qual é o objetivo principal do site?",
        )

        payload = process_chat_request(
            chat_request=chat_request,
            tenant=self.tenant,
            session_id=session_id,
            user_message=self.message,
        )
        conversation.refresh_from_db()
        lead = LeadDraft.objects.filter(conversation=conversation).first()
        return {
            "reply": payload["reply"],
            "intent": payload.get("intent"),
            "ai_mode": payload.get("ai_mode"),
            "lead_state": conversation.lead_state,
            "collection_active": bool((lead.qualification_data or {}).get("collection_active")) if lead else False,
            "need_summary": str(getattr(lead, "need_summary", "") or ""),
        }

    @override_settings(LIVIA_AI_ENABLED=False, LIVIA_CHAT_RATE_LIMIT_ENABLED=False)
    def test_mode_a_ai_off_uses_deterministic_path(self):
        snap = self._deterministic_snapshot(session_id="dual-off")
        self.assertIsNone(snap["ai_mode"])
        self.assertIn("loja virtual", snap["reply"].lower())
        self.assertEqual(snap["lead_state"], "discovery")
        self.assertFalse(snap["collection_active"])

    @override_settings(
        LIVIA_AI_ENABLED=True,
        LIVIA_AI_DRY_RUN=False,
        LIVIA_OPENAI_API_KEY="test-key-local",
        LIVIA_CHAT_RATE_LIMIT_ENABLED=False,
        RUNNING_TESTS=False,
    )
    def test_mode_b_ai_on_mocked_preserves_state_and_uses_llm_text(self):
        from assistant_core.services.chat_processing import process_chat_request
        from assistant_core.services.chat_idempotency import build_request_fingerprint
        from assistant_core.services.openai_grounded_conversation import OpenAIConversationResult
        from conversations.models import ChatRequest

        session_id = "dual-on"
        conversation = Conversation.objects.create(tenant=self.tenant, session_id=session_id)
        request_id = uuid.uuid4()
        fingerprint = build_request_fingerprint(
            tenant_slug=self.tenant.slug,
            session_id=session_id,
            request_id=request_id,
            message=self.message,
        )
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            session_id=session_id,
            request_id=request_id,
            request_fingerprint=fingerprint,
            status=ChatRequest.Status.PROCESSING,
        )
        Message.objects.create(conversation=conversation, role=Message.Role.USER, content="preciso de um site")
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="Claro. Qual é o objetivo principal do site?",
        )

        llm_text = "Perfeito, uma loja virtual pode ser um ótimo caminho. Me conta o porte do catálogo inicial."
        with patch(
            "assistant_core.services.openai_grounded_conversation.OpenAIGroundedConversationService.generate",
            return_value=OpenAIConversationResult(text=llm_text, used=True, status="completed", model="gpt-4.1-mini"),
        ) as generate_mock:
            payload = process_chat_request(
                chat_request=chat_request,
                tenant=self.tenant,
                session_id=session_id,
                user_message=self.message,
            )

        self.assertTrue(generate_mock.called)
        self.assertEqual(payload["reply"], llm_text)
        self.assertEqual(payload.get("ai_mode"), "openai_conversation")
        conversation.refresh_from_db()
        lead = LeadDraft.objects.filter(conversation=conversation).first()
        self.assertEqual(conversation.lead_state, "discovery")
        self.assertFalse((lead.qualification_data or {}).get("collection_active") if lead else True)

    @override_settings(
        LIVIA_AI_ENABLED=True,
        LIVIA_AI_DRY_RUN=False,
        LIVIA_OPENAI_API_KEY="test-key-local",
        LIVIA_CHAT_RATE_LIMIT_ENABLED=False,
        RUNNING_TESTS=False,
    )
    def test_mode_b_openai_timeout_fallback_preserves_state(self):
        from assistant_core.services.chat_processing import process_chat_request
        from assistant_core.services.chat_idempotency import build_request_fingerprint
        from assistant_core.services.openai_grounded_conversation import OpenAIConversationResult
        from conversations.models import ChatRequest

        session_id = "dual-fallback"
        conversation = Conversation.objects.create(tenant=self.tenant, session_id=session_id)
        request_id = uuid.uuid4()
        fingerprint = build_request_fingerprint(
            tenant_slug=self.tenant.slug,
            session_id=session_id,
            request_id=request_id,
            message=self.message,
        )
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            session_id=session_id,
            request_id=request_id,
            request_fingerprint=fingerprint,
            status=ChatRequest.Status.PROCESSING,
        )
        Message.objects.create(conversation=conversation, role=Message.Role.USER, content="preciso de um site")
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="Claro. Qual é o objetivo principal do site?",
        )

        with patch(
            "assistant_core.services.openai_grounded_conversation.OpenAIGroundedConversationService.generate",
            return_value=OpenAIConversationResult(status="failed", skip_reason="Timeout", error_type="Timeout"),
        ):
            payload = process_chat_request(
                chat_request=chat_request,
                tenant=self.tenant,
                session_id=session_id,
                user_message=self.message,
            )

        self.assertIn("loja virtual", payload["reply"].lower())
        self.assertTrue(payload.get("observability", {}).get("ai_fallback_used"))
        conversation.refresh_from_db()
        lead = LeadDraft.objects.filter(conversation=conversation).first()
        self.assertEqual(conversation.lead_state, "discovery")
        self.assertFalse((lead.qualification_data or {}).get("collection_active") if lead else True)
