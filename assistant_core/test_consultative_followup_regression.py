from __future__ import annotations

import json
import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from assistant_core.consultative_slots import (
    extract_consultative_slots,
    select_cleaning_followup,
    should_skip_followup_for_answered_slots,
)
from assistant_core.conversation_turns import TurnKind, classify_conversation_turn, is_direct_question, normalize_text
from assistant_core.discovery import analyze_message
from assistant_core.dialogue_memory import DialogueMemory
from assistant_core.followup_strategy import select_followup
from assistant_core.services.deterministic_synthesis import synthesize_deterministic_reply
from assistant_core.services.livia_decision import LiviaDecisionService
from assistant_core.state import LeadState, next_state_after_message
from conversations.models import Conversation, HandoffRequest
from knowledge_base.models import KnowledgeDocument, TenantRagConfiguration
from knowledge_base.rag.indexing import run_index_for_tenant
from knowledge_base.rag.sync import run_chunk_build_for_tenant
from knowledge_base.services.manual_rag import sync_manual_knowledge_document_to_rag
from knowledge_base.rag.embeddings import FakeEmbeddingProvider, load_embedding_config
from leads.models import LeadDraft
from tenants.models import AssistantProfile, Tenant, TenantAllowedOrigin


HYGIBOT_KB = """
# HygiBot / Dune Bot — robô de limpeza autônoma

Nome oficial: HygiBot / Dune Bot
Categoria: limpeza profissional

## Aplicação
Apoiar rotinas de limpeza em grandes áreas, com modos de lavar, varrer, aspirar e passar pano conforme ambiente e operação.

## Ambientes citados no site
- shoppings;
- indústrias;
- hospitais;
- grandes áreas.

## Limites
A escolha depende de tipo de piso, fluxo de pessoas, horários, obstáculos e responsáveis operacionais.
"""


@override_settings(
    ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"],
    LIVIA_AI_ENABLED=False,
    LIVIA_CHAT_RATE_LIMIT_ENABLED=False,
    LIVIA_LEAD_NOTIFICATIONS_ENABLED=False,
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
    LIVIA_RAG_MAX_RETRIEVED_CHUNKS=3,
    LIVIA_RAG_MAX_CONTEXT_CHARS=1200,
    LIVIA_RAG_MAX_CHUNKS_PER_MANIFEST=2,
)
class ConsultativeFollowupRegressionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="smart-control-brasil-followup",
            domain="https://scb-followup.example",
            is_active=True,
        )
        TenantAllowedOrigin.objects.create(tenant=self.tenant, origin="https://scb-followup.example")
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            initial_message="Olá, eu sou a Lívia da Smart Control Brasil.",
            business_domain="automação, robótica e sistemas web",
        )
        self.provider = FakeEmbeddingProvider()
        self.embedding_config = load_embedding_config()
        self._ingest(
            title="HygiBot Dune Limpeza",
            content=HYGIBOT_KB,
        )
        self._ingest(
            title="Xyron Visão Geral",
            content=(
                "A Smart Control Brasil trabalha com robótica de serviço e IA da linha Xyron Robotics. "
                "Visão geral dos produtos oficiais."
            ),
        )
        self.session_id = f"staging-cleaning-{uuid.uuid4().hex[:8]}"

    def _ingest(self, *, title: str, content: str):
        document = KnowledgeDocument.objects.create(
            tenant=self.tenant,
            title=title,
            slug=f"{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
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

    def _chat(self, message: str) -> dict:
        rid = str(uuid.uuid4())
        response = self.client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "tenant": self.tenant.slug,
                    "session_id": self.session_id,
                    "request_id": rid,
                    "message": message,
                    "source_page": "https://scb-followup.example",
                }
            ),
            content_type="application/json",
            HTTP_ORIGIN="https://scb-followup.example",
            HTTP_X_LIVIA_REQUEST_ID=rid,
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_classify_pronoun_direct_question_before_enrichment(self):
        tenant = Tenant.objects.create(name="T", slug="t-pronoun", domain="t.example")
        conversation = Conversation.objects.create(
            tenant=tenant,
            session_id="pronoun",
            lead_state=LeadState.DISCOVERY,
        )
        LeadDraft.objects.create(
            tenant=tenant,
            conversation=conversation,
            need_summary="preciso de um robo de limpeza. um galpão. 3000 m2, piso de concreto",
        )
        discovery = analyze_message("ele consegue trabalhar com pessoas circulando?")
        turn = classify_conversation_turn(
            current_message="ele consegue trabalhar com pessoas circulando?",
            history=[
                {"role": "user", "content": "preciso de um robo de limpeza"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "um galpão"},
            ],
            conversation=conversation,
            discovery=discovery,
        )
        self.assertEqual(turn.kind, TurnKind.DIRECT_QUESTION)
        self.assertTrue(is_direct_question("ele consegue trabalhar com pessoas circulando?"))

    def test_anti_repeat_skips_environment_floor_when_known(self):
        history = [
            {"role": "user", "content": "preciso de um robo de limpeza"},
            {"role": "user", "content": "um galpão"},
            {"role": "user", "content": "3000 m2, piso de concreto"},
        ]
        need = "preciso de um robo de limpeza. um galpão. 3000 m2, piso de concreto"
        slots = extract_consultative_slots(need_summary=need, history=history, current_message="")
        self.assertTrue(slots.environment_type)
        self.assertTrue(slots.floor_surface)
        self.assertTrue(slots.area_size)
        followup = select_cleaning_followup(slots=slots, current_message="")
        self.assertNotIn("ambiente e o tipo de piso", followup.lower())
        self.assertTrue(
            should_skip_followup_for_answered_slots(
                "Qual é o ambiente e o tipo de piso onde a limpeza acontece?",
                need_summary=need,
                history=history,
                current_message="",
            )
        )

    def test_lead_state_stays_discovery_during_consultative_flow(self):
        self._chat("preciso de um robo de limpeza")
        self._chat("um galpão")
        self._chat("3000 m2, piso de concreto")
        conversation = Conversation.objects.get(session_id=self.session_id)
        lead = LeadDraft.objects.get(conversation=conversation)
        self.assertEqual(conversation.lead_state, LeadState.DISCOVERY)
        self.assertFalse((lead.qualification_data or {}).get("collection_active"))
        snapshot = next_state_after_message(conversation, lead, intent="commercial_interest")
        self.assertEqual(snapshot.state, LeadState.DISCOVERY)

    def test_synthesis_prefers_hygibot_over_xyron_overview(self):
        synthesized = synthesize_deterministic_reply(
            HYGIBOT_KB
            + "\n\nA Smart Control Brasil trabalha com robótica de serviço e IA da linha Xyron Robotics.",
            base_reply="",
            current_message="preciso de um robo de limpeza",
            active_domain="robotics",
            active_application="cleaning_robotics",
        ).lower()
        self.assertTrue(any(token in synthesized for token in ("hygibot", "dune", "limpeza", "lavar", "varrer")))
        self.assertNotIn("visão geral dos produtos", synthesized)

    def _assert_consultative_state_invariants(self, *, message: str):
        conversation = Conversation.objects.get(session_id=self.session_id)
        lead = LeadDraft.objects.get(conversation=conversation)
        self.assertEqual(conversation.lead_state, LeadState.DISCOVERY, message)
        self.assertFalse((lead.qualification_data or {}).get("collection_active"), message)
        self.assertFalse((lead.qualification_data or {}).get("commercial_intent"), message)
        self.assertEqual(HandoffRequest.objects.filter(conversation=conversation).count(), 0, message)

    def test_four_turn_staging_regression_sequence(self):
        messages = (
            "preciso de um robo de limpeza",
            "um galpão",
            "3000 m2, piso de concreto",
            "ele consegue trabalhar com pessoas circulando?",
        )
        replies: list[str] = []
        for message in messages:
            payload = self._chat(message)
            replies.append(payload["reply"])
            self._assert_consultative_state_invariants(message=message)

        self.assertNotIn("educacional", replies[0].lower())
        self.assertNotIn("liro", replies[0].lower())
        self.assertFalse(
            replies[0].strip().lower().startswith("entendi, isso ajuda")
            and not any(token in replies[0].lower() for token in ("hygibot", "dune", "limpeza", "lavar", "varrer", "aspirar")),
            replies[0],
        )
        self.assertTrue(
            any(token in replies[0].lower() for token in ("hygibot", "dune", "limpeza", "lavar", "varrer", "aspirar"))
        )
        self.assertTrue("?" in replies[0])

        self.assertNotIn("ambiente e o tipo de piso", replies[1].lower())

        self.assertNotIn("ambiente e o tipo de piso", replies[2].lower())
        self.assertNotIn("tipo de piso", replies[2].lower())
        self.assertNotIn("metragem", replies[2].lower())
        from assistant_core.services.response_quality_gate import is_acknowledgement_only_reply

        self.assertFalse(is_acknowledgement_only_reply(replies[2]), replies[2])
        self.assertTrue(
            any(token in replies[2].lower() for token in ("hygibot", "dune", "limpeza", "lavar", "varrer", "fluxo", "circul", "frequ"))
            or "?" in replies[2],
            replies[2],
        )

        reply4 = replies[3].lower()
        self.assertNotIn("ambiente e o tipo de piso", reply4)
        self.assertTrue(
            any(token in reply4 for token in ("fluxo", "pessoas", "circul", "confirmação", "documentação", "operacao", "operação"))
            or "?" not in reply4,
            replies[3],
        )

        normalized = [reply.strip().lower() for reply in replies]
        self.assertGreater(len(set(normalized)), 1)

        conversation = Conversation.objects.get(session_id=self.session_id)
        lead = LeadDraft.objects.get(conversation=conversation)
        self.assertEqual(conversation.lead_state, LeadState.DISCOVERY)
        self.assertFalse((lead.qualification_data or {}).get("collection_active"))
        self.assertFalse(lead.name)
        self.assertFalse(lead.company)
        self.assertFalse(lead.phone)
        self.assertFalse(lead.email)

    def test_consultative_conversation_does_not_double_synthesize(self):
        """Resposta já grounded não passa novamente por _with_knowledge."""
        service = LiviaDecisionService()
        tenant = Tenant.objects.create(name="T", slug="t-double", domain="t.example")
        conversation = Conversation.objects.create(
            tenant=tenant,
            session_id="double-synth",
            lead_state=LeadState.DISCOVERY,
        )
        LeadDraft.objects.create(
            tenant=tenant,
            conversation=conversation,
            need_summary="preciso de um robo de limpeza",
        )
        discovery = analyze_message("preciso de um robo de limpeza")
        grounded = "Apoiar rotinas de limpeza em grandes áreas, com modos de lavar, varrer e aspirar."
        kb = "[KNOWLEDGE_BASE]\n" + HYGIBOT_KB + "\n\nA Smart Control Brasil trabalha com robótica de serviço e IA da linha Xyron Robotics."
        memory = DialogueMemory(
            active_domain="robotics",
            active_topic="cleaning_robot",
            active_application="cleaning_robotics",
            active_entity="HygiBot",
        )

        with patch.object(service, "_build_grounded_consultative_reply", return_value=grounded) as compose_mock:
            with patch.object(service, "_with_knowledge", wraps=service._with_knowledge) as with_knowledge_mock:
                with patch(
                    "assistant_core.services.deterministic_synthesis.synthesize_deterministic_reply",
                    wraps=synthesize_deterministic_reply,
                ) as synthesize_mock:
                    reply = service._handle_consultative_conversation(
                        intent="commercial_interest",
                        history=[],
                        current_message="preciso de um robo de limpeza",
                        conversation=conversation,
                        discovery=discovery,
                        assistant_profile=None,
                        knowledge_context=kb,
                        dialogue_memory=memory,
                    )

        compose_mock.assert_called_once()
        with_knowledge_mock.assert_not_called()
        self.assertEqual(synthesize_mock.call_count, 0)
        self.assertIn("limpeza", reply.reply.lower())
        self.assertNotIn("visão geral dos produtos", reply.reply.lower())

    def test_same_turn_dialogue_memory_drives_synthesis_before_persist(self):
        """Memória do turno atual alimenta síntese sem depender de persistência prévia."""
        service = LiviaDecisionService()
        tenant = Tenant.objects.create(name="T", slug="t-memory", domain="t.example")
        conversation = Conversation.objects.create(
            tenant=tenant,
            session_id="same-turn-memory",
            lead_state=LeadState.DISCOVERY,
        )
        lead = LeadDraft.objects.create(
            tenant=tenant,
            conversation=conversation,
            need_summary="preciso de um robo de limpeza",
            qualification_data={},
        )
        stale = DialogueMemory(active_domain="", active_topic="", active_application="", active_entity="")
        current = DialogueMemory(
            active_domain="robotics",
            active_topic="cleaning_robot",
            active_application="cleaning_robotics",
            active_entity="HygiBot",
        )
        kb = "[KNOWLEDGE_BASE]\n" + HYGIBOT_KB

        with patch("assistant_core.dialogue_memory.load_dialogue_memory", return_value=stale) as load_mock:
            with patch(
                "assistant_core.services.livia_decision.synthesize_deterministic_reply",
                wraps=synthesize_deterministic_reply,
            ) as synth_mock:
                reply = service._build_grounded_consultative_reply(
                    lead_draft=lead,
                    conversation=conversation,
                    current_message="preciso de um robo de limpeza",
                    history=[],
                    knowledge_context=kb,
                    append_followup=True,
                    dialogue_memory=current,
                )

        load_mock.assert_not_called()
        self.assertGreaterEqual(synth_mock.call_count, 1)
        first_kwargs = synth_mock.call_args_list[0].kwargs
        self.assertEqual(first_kwargs.get("active_application"), "cleaning_robotics")
        synthesis_query = normalize_text(first_kwargs.get("current_message", ""))
        self.assertTrue(
            "hygibot" in synthesis_query or "limpeza profissional" in synthesis_query,
            synthesis_query,
        )
        self.assertTrue(
            any(token in reply.lower() for token in ("hygibot", "dune", "limpeza", "lavar", "varrer", "aspirar")),
            reply,
        )
        self.assertTrue("?" in reply)

    def test_grounded_first_composes_evidence_before_followup(self):
        service = LiviaDecisionService()
        memory = DialogueMemory(
            active_domain="robotics",
            active_topic="cleaning_robot",
            active_application="cleaning_robotics",
            active_entity="HygiBot",
        )
        kb = (
            "[KNOWLEDGE_BASE]\n"
            + HYGIBOT_KB
            + "\n\nA Smart Control Brasil trabalha com robótica de serviço e IA da linha Xyron Robotics."
        )
        reply = service._build_grounded_consultative_reply(
            lead_draft=None,
            conversation=None,
            current_message="preciso de um robo de limpeza",
            history=[],
            knowledge_context=kb,
            append_followup=True,
            dialogue_memory=memory,
        )
        lowered = reply.lower()
        self.assertTrue(
            any(token in lowered for token in ("hygibot", "dune", "limpeza", "lavar", "varrer", "aspirar")),
            reply,
        )
        self.assertNotIn("visão geral dos produtos", lowered)
        self.assertTrue("?" in reply)
        from assistant_core.services.response_quality_gate import is_acknowledgement_only_reply

        self.assertFalse(is_acknowledgement_only_reply(reply), reply)

    def test_acknowledgement_only_variants_detected_and_repaired(self):
        from assistant_core.services.response_quality_gate import (
            apply_response_quality_gate,
            is_acknowledgement_only_reply,
        )

        memory = DialogueMemory(
            active_domain="robotics",
            active_topic="cleaning_robot",
            active_application="cleaning_robotics",
            active_entity="HygiBot",
        )
        ack_variants = (
            "Entendi.",
            "Certo.",
            "Perfeito.",
            "Entendi, isso ajuda a detalhar a necessidade.",
        )
        for ack in ack_variants:
            with self.subTest(ack=ack):
                self.assertTrue(is_acknowledgement_only_reply(ack), ack)
                repaired, diagnostics = apply_response_quality_gate(
                    reply=ack,
                    knowledge_context=HYGIBOT_KB,
                    current_message="3000 m2, piso de concreto",
                    memory=memory,
                    need_summary="preciso de um robo de limpeza. um galpão. 3000 m2, piso de concreto",
                    history=[],
                    append_followup=False,
                )
                self.assertFalse(is_acknowledgement_only_reply(repaired), repaired)
                self.assertTrue(
                    diagnostics.get("regrounded")
                    or any(
                        token in repaired.lower()
                        for token in ("limpeza", "hygibot", "lavar", "varrer", "fluxo", "circul", "?")
                    ),
                    repaired,
                )

    def test_substantive_ack_prefix_not_classified_as_ack_only(self):
        from assistant_core.services.response_quality_gate import is_acknowledgement_only_reply

        substantive = (
            "Entendi. Para áreas amplas, a solução recuperada na base é adequada a rotinas de "
            "limpeza com modos de lavar, varrer e aspirar. Qual é o tipo de piso?"
        )
        self.assertFalse(is_acknowledgement_only_reply(substantive), substantive)

    def test_quality_gate_rejects_acknowledgement_only_with_knowledge(self):
        from assistant_core.services.response_quality_gate import (
            apply_response_quality_gate,
            is_acknowledgement_only_reply,
        )

        memory = DialogueMemory(
            active_domain="robotics",
            active_topic="cleaning_robot",
            active_application="cleaning_robotics",
        )
        reply, diagnostics = apply_response_quality_gate(
            reply="Entendi, isso ajuda a detalhar a necessidade.",
            knowledge_context=HYGIBOT_KB,
            current_message="3000 m2, piso de concreto",
            memory=memory,
            need_summary="preciso de um robo de limpeza. um galpão. 3000 m2, piso de concreto",
            history=[],
            append_followup=False,
        )
        self.assertFalse(is_acknowledgement_only_reply(reply), reply)
        self.assertTrue(diagnostics.get("regrounded") or any(token in reply.lower() for token in ("limpeza", "hygibot", "fluxo", "circul")))

    def test_anti_repeat_followup_steps_via_slots(self):
        memory = DialogueMemory(
            active_domain="robotics",
            active_topic="cleaning_robot",
            active_application="cleaning_robotics",
        )
        need = "preciso de um robo de limpeza"
        history: list[dict[str, str]] = []

        follow1, _ = select_followup(
            memory=memory,
            current_message="preciso de um robo de limpeza",
            need_summary=need,
            history=history,
            force=False,
        )
        self.assertIn("ambiente", follow1.lower())

        history.append({"role": "user", "content": "preciso de um robo de limpeza"})
        need = "preciso de um robo de limpeza. um galpão"
        follow2, _ = select_followup(
            memory=memory,
            current_message="um galpão",
            need_summary=need,
            history=history,
            force=False,
        )
        self.assertNotIn("ambiente e o tipo de piso", follow2.lower())

        history.append({"role": "user", "content": "um galpão"})
        need = "preciso de um robo de limpeza. um galpão. 3000 m2, piso de concreto"
        follow3, _ = select_followup(
            memory=memory,
            current_message="3000 m2, piso de concreto",
            need_summary=need,
            history=history,
            force=False,
        )
        lowered = follow3.lower()
        self.assertNotIn("ambiente e o tipo de piso", lowered)
        self.assertNotIn("metragem", lowered)
        self.assertNotIn("tipo de piso", lowered)

    def test_direct_question_via_api_answers_or_limits(self):
        self._chat("preciso de um robo de limpeza")
        self._chat("um galpão")
        self._chat("3000 m2, piso de concreto")
        conversation = Conversation.objects.get(session_id=self.session_id)
        history = [
            {"role": "user", "content": "preciso de um robo de limpeza"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "um galpão"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "3000 m2, piso de concreto"},
        ]
        message = "ele consegue trabalhar com pessoas circulando?"
        discovery = analyze_message(message)
        turn = classify_conversation_turn(
            current_message=message,
            history=history,
            conversation=conversation,
            discovery=discovery,
        )
        self.assertEqual(turn.kind, TurnKind.DIRECT_QUESTION)

        payload = self._chat(message)
        reply = payload["reply"].lower()
        self.assertNotIn("ambiente e o tipo de piso", reply)
        self.assertTrue(
            any(token in reply for token in ("fluxo", "pessoas", "circul", "confirmação", "documentação", "operacao", "operação"))
            or "?" not in reply,
            payload["reply"],
        )

    def test_lead_state_per_turn_and_budget_trigger(self):
        consultative_messages = (
            "preciso de um robo de limpeza",
            "um galpão",
            "3000 m2, piso de concreto",
            "ele consegue trabalhar com pessoas circulando?",
        )
        for message in consultative_messages:
            self._chat(message)
            conversation = Conversation.objects.get(session_id=self.session_id)
            lead = LeadDraft.objects.get(conversation=conversation)
            self.assertEqual(conversation.lead_state, LeadState.DISCOVERY, message)
            self.assertFalse((lead.qualification_data or {}).get("collection_active"), message)
            self.assertFalse((lead.qualification_data or {}).get("commercial_intent"), message)

        budget = self._chat("quero um orçamento")
        lead = LeadDraft.objects.get(conversation__session_id=self.session_id)
        self.assertTrue((lead.qualification_data or {}).get("collection_active"))
        self.assertTrue((lead.qualification_data or {}).get("commercial_intent"))
        lowered = budget["reply"].lower()
        self.assertTrue(
            any(token in lowered for token in ("chamar", "nome", "empresa", "telefone", "e-mail", "email", "whatsapp")),
            budget["reply"],
        )
