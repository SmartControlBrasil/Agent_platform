from __future__ import annotations

import json
import uuid

from django.core.cache import cache
from django.test import TestCase, override_settings

from assistant_core.consultative_policy import decide_collection, detect_collection_trigger
from assistant_core.conversation_turns import classify_conversation_turn, is_consultative_context_answer
from assistant_core.discovery import analyze_message
from assistant_core.qualification import infer_pending_field_values, is_valid_name
from assistant_core.services.deterministic_synthesis import synthesize_deterministic_reply
from conversations.models import Conversation
from knowledge_base.models import KnowledgeDocument, TenantRagConfiguration
from knowledge_base.rag.context_builder import build_knowledge_context_result
from knowledge_base.rag.conversation_retrieval import retrieve_context
from knowledge_base.rag.embeddings import FakeEmbeddingProvider, load_embedding_config
from knowledge_base.rag.indexing import run_index_for_tenant
from knowledge_base.rag.sync import run_chunk_build_for_tenant
from knowledge_base.services.manual_rag import sync_manual_knowledge_document_to_rag
from leads.models import LeadDraft
from tenants.models import AssistantProfile, Tenant, TenantAllowedOrigin


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
class ConsultativeCleaningRobotRegressionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="smart-control-brasil-regression",
            domain="https://scb-regression.example",
            is_active=True,
        )
        self.other_tenant = Tenant.objects.create(
            name="Outro Tenant",
            slug="other-tenant-regression",
            domain="https://other.example",
            is_active=True,
        )
        TenantAllowedOrigin.objects.create(tenant=self.tenant, origin="https://scb-regression.example")
        TenantAllowedOrigin.objects.create(tenant=self.other_tenant, origin="https://other.example")
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            initial_message="Olá, eu sou a Lívia da Smart Control Brasil.",
            business_domain="automação, robótica e sistemas web",
        )
        self.provider = FakeEmbeddingProvider()
        self.embedding_config = load_embedding_config()
        self._ingest(
            tenant=self.tenant,
            title="LIRO Educacional",
            content=(
                "Robô interativo para aproximar crianças e jovens da tecnologia por meio de "
                "experiências educacionais com comunicação, movimento e interação. "
                "Estudantes, escolas e instituições de ensino."
            ),
        )
        self._ingest(
            tenant=self.tenant,
            title="HygiBot Dune Limpeza",
            content=(
                "Robô autônomo de limpeza de pisos para galpões, shoppings e facilities. "
                "Lava, varre e aspira pisos de concreto, epóxi e porcelanato em áreas amplas."
            ),
        )
        self._ingest(
            tenant=self.other_tenant,
            title="LIRO Educacional Outro Tenant",
            content="Robô educacional exclusivo do outro tenant.",
        )
        self.session_id = f"scb-clean-{uuid.uuid4().hex[:8]}"

    def _ingest(self, *, tenant, title: str, content: str):
        document = KnowledgeDocument.objects.create(
            tenant=tenant,
            title=title,
            slug=f"{title.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
            content=content,
            status=KnowledgeDocument.Status.ACTIVE,
        )
        sync_manual_knowledge_document_to_rag(document=document)
        configuration = TenantRagConfiguration.objects.get(tenant=tenant)
        configuration.retrieval_enabled = True
        configuration.save(update_fields=["retrieval_enabled", "updated_at"])
        run_chunk_build_for_tenant(configuration=configuration)
        run_index_for_tenant(
            configuration=configuration,
            provider=self.provider,
            config=self.embedding_config,
            run_id=f"idx-{tenant.slug}-{document.pk}",
        )

    def _chat(self, message: str, *, session_id: str | None = None, tenant_slug: str | None = None) -> dict:
        slug = tenant_slug or self.tenant.slug
        origin = "https://scb-regression.example" if slug == self.tenant.slug else "https://other.example"
        rid = str(uuid.uuid4())
        response = self.client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "tenant": slug,
                    "session_id": session_id or self.session_id,
                    "request_id": rid,
                    "message": message,
                    "source_page": origin,
                }
            ),
            content_type="application/json",
            HTTP_ORIGIN=origin,
            HTTP_X_LIVIA_REQUEST_ID=rid,
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _assert_no_contact_collection(self, reply: str):
        lowered = reply.lower()
        self.assertNotIn("telefone/whatsapp", lowered)
        self.assertNotIn("me passa seu telefone", lowered)
        self.assertNotIn("e-mail para eu continuar", lowered)

    def test_scenario_1_consultative_flow_without_contact(self):
        first = self._chat("preciso de um robo de limpeza")
        reply1 = first["reply"].lower()
        self.assertNotIn("educacional", reply1)
        self.assertNotIn("criancas", reply1)
        self.assertNotIn("crianças", reply1)
        self.assertNotIn("liro", reply1)
        self.assertTrue(
            any(token in reply1 for token in ("limpeza", "galp", "piso", "facilities", "hygibot", "dune", "rob", "ambiente"))
        )

        second = self._chat("um galpão")
        self._assert_no_contact_collection(second["reply"])
        self.assertTrue(
            "?" in second["reply"]
            or any(token in second["reply"].lower() for token in ("metragem", "piso", "m2", "concreto", "ambiente"))
        )

        third = self._chat("3000 m2, concreto")
        self._assert_no_contact_collection(third["reply"])
        lead = LeadDraft.objects.get(conversation__session_id=self.session_id)
        self.assertFalse((lead.qualification_data or {}).get("collection_active"))

    def test_scenario_2_commercial_trigger_only_on_explicit_budget(self):
        for message in ("preciso de um robo de limpeza", "um galpão", "3000 m2"):
            payload = self._chat(message)
            self._assert_no_contact_collection(payload["reply"])

        budget = self._chat("quero um orçamento")
        lowered = budget["reply"].lower()
        self.assertTrue(
            any(token in lowered for token in ("chamar", "nome", "empresa", "telefone", "e-mail", "email", "whatsapp")),
            budget["reply"],
        )
        lead = LeadDraft.objects.get(conversation__session_id=self.session_id)
        self.assertTrue((lead.qualification_data or {}).get("collection_active"))

    def test_scenario_3_company_name_after_commercial_trigger(self):
        self._chat("preciso de um robo de limpeza")
        self._chat("quero um orçamento")
        company = self._chat("Grupo Mecanismo")
        lowered = company["reply"].lower()
        self.assertNotIn("grupomecanismo", lowered)
        lead = LeadDraft.objects.get(conversation__session_id=self.session_id)
        self.assertEqual(lead.company, "Grupo Mecanismo")
        self.assertEqual(lead.name, "Grupo Mecanismo")
        self.assertTrue(
            "telefone" in lowered or "whatsapp" in lowered or "e-mail" in lowered or "email" in lowered,
            company["reply"],
        )

    def test_scenario_4_phone_recognized_without_repeat(self):
        self._chat("preciso de um robo de limpeza")
        self._chat("quero um orçamento")
        self._chat("Grupo Mecanismo")
        phone = self._chat("11974587458")
        lead = LeadDraft.objects.get(conversation__session_id=self.session_id)
        self.assertTrue(str(lead.phone or "").endswith("974587458") or "11974587458" in str(lead.phone))
        self.assertNotIn("me passa seu telefone", phone["reply"].lower())

    def test_scenario_5_rag_prioritizes_cleaning_over_educational(self):
        from knowledge_base.rag.content_classification import infer_robotics_family, robotics_families_compatible

        self.assertEqual(
            infer_robotics_family(text="preciso de um robo de limpeza", application="cleaning_robotics"),
            "cleaning",
        )
        self.assertFalse(
            robotics_families_compatible(
                "cleaning",
                "Robô interativo para aproximar crianças e jovens da tecnologia educacional LIRO",
            )
        )
        self.assertTrue(
            robotics_families_compatible(
                "cleaning",
                "Robô autônomo de limpeza de pisos para galpões HygiBot Dune",
            )
        )

        result = build_knowledge_context_result(
            tenant=self.tenant,
            message="HygiBot Dune limpeza galpão",
            contextual_query="HygiBot Dune limpeza galpão facilities piso",
            active_domain="robotics",
            active_application="cleaning_robotics",
            active_entity="Duno",
            limit=2,
        )
        if result.text:
            context = result.text.lower()
            self.assertIn("limpeza", context)
            self.assertNotIn("educacional", context)

    def test_scenario_6_metadata_driven_new_product_without_hardcode(self):
        from knowledge_base.rag.entity_catalog import entity_catalog_for_tenant, resolve_knowledge_entity

        novel_title = f"ORBIT-X9 Manual {uuid.uuid4().hex[:4]}"
        self._ingest(
            tenant=self.tenant,
            title=novel_title,
            content=(
                "ORBIT-X9\n"
                "Plataforma móvel autônoma para inspeção de corredores industriais.\n"
                "Indicada para inspeção visual em ambientes logísticos."
            ),
        )
        catalog_names = {entity.canonical_name.upper() for entity in entity_catalog_for_tenant(self.tenant)}
        self.assertTrue(any("ORBIT-X9" in name for name in catalog_names))

        resolution = resolve_knowledge_entity(
            tenant=self.tenant,
            message="preciso de um ORBIT-X9 para inspeção",
        )
        self.assertIsNotNone(resolution.subject)
        self.assertIn("ORBIT-X9", resolution.subject["canonical_name"].upper())

        payload = self._chat("preciso de um ORBIT-X9 para inspeção")
        self.assertIn("orbit-x9", payload["reply"].lower())

    def test_scenario_7_pronoun_followup_keeps_cleaning_subject(self):
        self._chat("fale sobre o robô de limpeza")
        follow = self._chat("ele funciona em galpão?")
        reply = follow["reply"].lower()
        self.assertNotIn("educacional", reply)
        self.assertNotIn("liro", reply)

    def test_scenario_8_tenant_isolation(self):
        result = retrieve_context(
            tenant=self.tenant,
            query="preciso de um robo de limpeza",
            provider=self.provider,
            config=self.embedding_config,
        )
        joined = " ".join(chunk.text for chunk in result.chunks).lower()
        self.assertNotIn("exclusivo do outro tenant", joined)

    def test_qualification_guards_environment_and_company_inference(self):
        self.assertFalse(is_valid_name("preciso de um robo de limpeza"))
        self.assertFalse(is_valid_name("um galpão"))
        self.assertTrue(is_consultative_context_answer("um galpão"))
        self.assertEqual(infer_pending_field_values("um galpão", "name_or_company"), {})
        self.assertEqual(
            infer_pending_field_values("grupomecanismo", "name_or_company"),
            {"company": "Grupo Mecanismo"},
        )

    def test_decide_collection_stays_consultative_for_environment_answer(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="env-ctx")
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            need_summary="preciso de um robo de limpeza",
            qualification_data={"collection_active": True},
        )
        discovery = analyze_message("um galpão")
        decision = decide_collection(
            current_message="um galpão",
            conversation=conversation,
            lead_draft=lead,
            discovery=discovery,
        )
        self.assertFalse(decision.should_collect)
        turn = classify_conversation_turn(
            current_message="um galpão",
            history=[{"role": "user", "content": "preciso de um robo de limpeza"}],
            conversation=conversation,
            discovery=discovery,
        )
        self.assertEqual(turn.kind.value, "need_enrichment")

    def test_synthesis_prefers_cleaning_bits(self):
        context = (
            "[KNOWLEDGE_BASE]\n"
            "Fonte: LIRO Educacional\n"
            "Conteúdo:\n"
            "Robô interativo para aproximar crianças e jovens da tecnologia.\n\n"
            "Fonte: HygiBot Dune Limpeza\n"
            "Conteúdo:\n"
            "Robô autônomo de limpeza de pisos para galpões e facilities.\n"
            "[/KNOWLEDGE_BASE]"
        )
        reply = synthesize_deterministic_reply(
            context,
            base_reply="",
            current_message="preciso de um robo de limpeza",
            active_application="cleaning_robotics",
        ).lower()
        self.assertIn("limpeza", reply)
        self.assertNotIn("educacional", reply)
