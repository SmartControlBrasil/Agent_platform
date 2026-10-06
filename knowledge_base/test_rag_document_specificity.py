from __future__ import annotations

import uuid

from django.test import TestCase, override_settings

from assistant_core.dialogue_memory import DialogueMemory, build_contextual_retrieval_query, update_dialogue_memory_from_turn
from assistant_core.services.deterministic_synthesis import synthesize_deterministic_reply
from knowledge_base.models import KnowledgeDocument, TenantRagConfiguration, TenantRagDriveFileManifest
from knowledge_base.rag.conversation_retrieval import retrieve_context
from knowledge_base.rag.embeddings import FakeEmbeddingProvider, load_embedding_config
from knowledge_base.rag.entity_catalog import entity_catalog_for_tenant, normalize_entity_text, resolve_knowledge_entity
from knowledge_base.rag.indexing import run_index_for_tenant
from knowledge_base.rag.sync import run_chunk_build_for_tenant
from knowledge_base.services.manual_rag import manual_drive_file_id, sync_manual_knowledge_document_to_rag
from knowledge_base.testing.rag_dimensions import RagTestDimensionMixin
from tenants.models import Tenant


XYRON_OVERVIEW = """
# Robótica de serviço Xyron

A Smart Control Brasil trabalha com robótica de serviço e IA da linha Xyron Robotics.

## Produtos oficiais no site (nomenclatura institucional)
- LIRO / Little Bot — robótica educacional interativa
- HygiBot / Dune Bot — limpeza profissional
- Orbit Bot / Patrol Bot — patrulhamento e segurança

## Orientação rápida
- Escola/educação → LIRO / Little Bot
- Limpeza → HygiBot / Dune Bot
- Segurança/patrulha → Orbit Bot / Patrol Bot
"""

HYGIBOT_DEDICATED = """
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

ORBIT_DEDICATED = """
# Orbit Bot / Patrol Bot — patrulhamento e segurança

Nome oficial: Orbit Bot / Patrol Bot
Categoria: segurança patrimonial / patrulhamento

## Aplicação
Apoiar rotinas de patrulhamento, monitoramento e presença operacional em grandes áreas.
"""

LIRO_DEDICATED = """
# LIRO / Little Bot — robô educacional interativo

Nome oficial: LIRO / Little Bot
Categoria: robótica educacional / interação

## Aplicação
Robô interativo para aproximar crianças e jovens da tecnologia por meio de experiências educacionais.

## Uso educacional
Apoia atividades pedagogicas, apresentacoes em sala e experiencias introdutorias de tecnologia.
A proposta comercial deve levantar objetivos, ambiente, quantidade de alunos e recursos esperados.
O atendimento consultivo confirma escopo antes de detalhar configuracoes tecnicas ou disponibilidade.

## Limites comerciais
Observacao comercial: autonomia e especificacoes tecnicas nao publicadas devem ser confirmadas em documento tecnico atualizado.
"""


LIRO_TECHNICAL_SPECS = """
# LIRO / Little Bot - ficha tecnica

Nome oficial: LIRO / Little Bot
Categoria: robotica educacional / interacao

## Garantia
Garantia do robo LIRO: 12 meses. Garantia da bateria: 6 meses. Garantia do carregador: 6 meses.

## Bateria e autonomia
Capacidade da bateria do LIRO: 6400 mAh. Autonomia estimada: ate 8 horas em uso educacional tipico.
"""

LIRO_NETWORK_REQUIREMENTS = """
# LIRO / Little Bot - requisitos de rede

Nome oficial: LIRO / Little Bot
Categoria: robotica educacional / interacao

## Wi-Fi
O LIRO opera em redes Wi-Fi 2.4 GHz. Redes Wi-Fi 5 GHz nao sao suportadas pelo modulo de conectividade.

## Operacao sem internet
Sem internet, recursos locais permanecem disponiveis, mas sincronizacao em nuvem e atualizacoes ficam indisponiveis.
"""


@override_settings(
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
class RagDocumentSpecificityTests(RagTestDimensionMixin, TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="SCB", slug=f"scb-spec-{uuid.uuid4().hex[:6]}")
        self.provider = FakeEmbeddingProvider()
        self.embedding_config = load_embedding_config()
        self.memory = DialogueMemory()
        self.manifests: dict[str, TenantRagDriveFileManifest] = {}

    def _ingest(self, *, slug: str, title: str, content: str) -> TenantRagDriveFileManifest:
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
        manifest = TenantRagDriveFileManifest.objects.get(
            tenant=self.tenant,
            drive_file_id=manual_drive_file_id(document),
        )
        self.manifests[slug] = manifest
        return manifest

    def _seed_xyron_family(self):
        self._ingest(slug="robotica_xyron_visao_geral", title="Xyron Visão Geral", content=XYRON_OVERVIEW)
        self._ingest(slug="hygibot_dune", title="Hygiibot Dune", content=HYGIBOT_DEDICATED)
        self._ingest(slug="orbit", title="Orbit Bot", content=ORBIT_DEDICATED)
        self._ingest(slug="liro_littlebot", title="LIRO Little Bot", content=LIRO_DEDICATED)


    def _legacy_liro_subject(self) -> dict:
        return {
            "confidence": 0.93,
            "entity_type": "product",
            "match_method": "application_family",
            "canonical_name": "LIRO / Little Bot",
            "source_document_ids": [
                self.manifests["liro_littlebot"].id,
                self.manifests["robotica_xyron_visao_geral"].id,
            ],
        }

    def _seed_liro_with_new_technical_documents(self):
        self._seed_xyron_family()
        self._ingest(slug="liro_ficha_tecnica", title="LIRO Ficha Tecnica", content=LIRO_TECHNICAL_SPECS)
        self._ingest(slug="liro_requisitos_rede", title="LIRO Requisitos de Rede", content=LIRO_NETWORK_REQUIREMENTS)

    def _retrieve_with_legacy_liro_subject(self, message: str, *, contextual_query: str | None = None):
        active_subject = self._legacy_liro_subject()
        return retrieve_context(
            tenant=self.tenant,
            query=message,
            contextual_query=contextual_query or f"LIRO / Little Bot {message}",
            active_subject=active_subject,
            active_application="educational_robotics",
            active_entity="LIRO",
            active_domain="robotics",
            provider=self.provider,
            config=self.embedding_config,
        )

    def _turn(self, message: str):
        self.memory = update_dialogue_memory_from_turn(
            memory=self.memory,
            current_message=message,
            history=[],
            tenant=self.tenant,
        )
        _original, contextual = build_contextual_retrieval_query(
            current_message=message,
            memory=self.memory,
            history=[],
        )
        result = retrieve_context(
            tenant=self.tenant,
            query=message,
            contextual_query=contextual,
            active_subject=self.memory.active_knowledge_subject,
            active_application=self.memory.active_application,
            active_entity=self.memory.active_entity,
            active_domain=self.memory.active_domain,
            provider=self.provider,
            config=self.embedding_config,
        )
        reply = synthesize_deterministic_reply(
            result.context_text,
            current_message=message,
            active_domain=self.memory.active_domain,
            active_application=self.memory.active_application,
        )
        return reply, result

    def test_catalog_does_not_create_orientation_entity(self):
        self._seed_xyron_family()
        names = {entity.canonical_name for entity in entity_catalog_for_tenant(self.tenant)}
        self.assertNotIn("Limpeza → HygiBot /", names)
        self.assertNotIn("Limpeza → HygiBot / Dune Bot", names)
        self.assertTrue(any("hygibot" in normalize_entity_text(name) for name in names))

    def test_cleaning_query_resolves_hygibot_with_specific_source_first(self):
        self._seed_xyron_family()
        resolution = resolve_knowledge_entity(
            tenant=self.tenant,
            message="preciso de um robo de limpeza",
            active_application="cleaning_robotics",
            active_topic="cleaning_robot",
        )
        self.assertIsNotNone(resolution.subject)
        canonical = resolution.subject["canonical_name"]
        self.assertIn("HygiBot", canonical)
        self.assertNotIn("→", canonical)
        doc_ids = resolution.subject["source_document_ids"]
        self.assertGreaterEqual(len(doc_ids), 1)
        hygibot_manifest = self.manifests["hygibot_dune"].id
        overview_manifest = self.manifests["robotica_xyron_visao_geral"].id
        self.assertEqual(doc_ids[0], hygibot_manifest)
        self.assertIn(overview_manifest, doc_ids)

    def test_cleaning_retrieval_prefers_dedicated_document(self):
        self._seed_xyron_family()
        _reply, result = self._turn("preciso de um robo de limpeza")
        hygibot_manifest = self.manifests["hygibot_dune"].id
        overview_manifest = self.manifests["robotica_xyron_visao_geral"].id
        self.assertTrue(result.chunks)
        self.assertEqual(result.document_ids_used[0], hygibot_manifest)
        self.assertIn(hygibot_manifest, result.document_ids_used)
        self.assertNotEqual(result.document_ids_used, [overview_manifest])

    def test_cleaning_reply_uses_dedicated_content(self):
        self._seed_xyron_family()
        reply, _result = self._turn("preciso de um robo de limpeza")
        lowered = reply.lower()
        self.assertTrue(any(token in lowered for token in ("lavar", "varrer", "aspirar", "grandes áreas", "grandes areas")))
        self.assertNotIn("visão geral dos produtos", lowered)

    def test_security_query_prefers_orbit_over_overview(self):
        self._seed_xyron_family()
        resolution = resolve_knowledge_entity(
            tenant=self.tenant,
            message="preciso de um robô para segurança",
            active_application="security_robotics",
            active_topic="security_robot",
        )
        self.assertIsNotNone(resolution.subject)
        self.assertIn("Orbit", resolution.subject["canonical_name"])
        self.assertEqual(
            resolution.subject["source_document_ids"][0],
            self.manifests["orbit"].id,
        )
        _reply, result = self._turn("preciso de um robô para segurança")
        self.assertEqual(result.document_ids_used[0], self.manifests["orbit"].id)

    def test_education_query_prefers_liro_over_overview(self):
        self._seed_xyron_family()
        resolution = resolve_knowledge_entity(
            tenant=self.tenant,
            message="preciso de robótica para escola",
            active_application="educational_robotics",
            active_topic="educational_robot",
        )
        self.assertIsNotNone(resolution.subject)
        self.assertIn("LIRO", resolution.subject["canonical_name"])
        self.assertEqual(
            resolution.subject["source_document_ids"][0],
            self.manifests["liro_littlebot"].id,
        )
        _reply, result = self._turn("preciso de robótica para escola")
        self.assertEqual(result.chunks[0].document_id, self.manifests["liro_littlebot"].id)
        self.assertIn(self.manifests["liro_littlebot"].id, result.document_ids_used)

    def test_fictitious_new_product_without_code_change(self):
        overview = (
            "# Linha ServiceBots\n\n"
            "## Produtos\n"
            "- Limpeza industrial → CleanMaster Z9\n"
            "- Logística → MoveBot Q2\n\n"
            "## Orientação\n"
            "- Limpeza → CleanMaster Z9\n"
        )
        dedicated = (
            "# CleanMaster Z9 — robô de limpeza industrial\n\n"
            "Nome oficial: CleanMaster Z9\n"
            "Categoria: limpeza industrial\n\n"
            "## Aplicação\n"
            "Apoiar limpeza pesada em pisos industriais com modos de lavagem e aspiração.\n"
        )
        self._ingest(slug="servicebots_overview", title="ServiceBots Overview", content=overview)
        self._ingest(slug="cleanmaster_z9", title="CleanMaster Z9", content=dedicated)
        resolution = resolve_knowledge_entity(
            tenant=self.tenant,
            message="preciso de um robô CleanMaster Z9",
            active_application="cleaning_robotics",
        )
        self.assertIsNotNone(resolution.subject)
        self.assertIn("CleanMaster", resolution.subject["canonical_name"])
        self.assertEqual(
            resolution.subject["source_document_ids"][0],
            self.manifests["cleanmaster_z9"].id,
        )
        _reply, result = self._turn("preciso de um robô CleanMaster Z9")
        self.assertEqual(result.document_ids_used[0], self.manifests["cleanmaster_z9"].id)

    def test_cleaning_ranking_prefers_dedicated_manifest(self):
        self._seed_xyron_family()
        self.memory = update_dialogue_memory_from_turn(
            memory=DialogueMemory(),
            current_message="preciso de um robo de limpeza",
            history=[],
            tenant=self.tenant,
        )
        _original, contextual = build_contextual_retrieval_query(
            current_message="preciso de um robo de limpeza",
            memory=self.memory,
            history=[],
        )
        result = retrieve_context(
            tenant=self.tenant,
            query="preciso de um robo de limpeza",
            contextual_query=contextual,
            active_subject=self.memory.active_knowledge_subject,
            active_application=self.memory.active_application,
            active_entity=self.memory.active_entity,
            active_domain=self.memory.active_domain,
            provider=self.provider,
            config=self.embedding_config,
        )
        hygibot_id = self.manifests["hygibot_dune"].id
        overview_id = self.manifests["robotica_xyron_visao_geral"].id
        self.assertEqual(self.memory.active_knowledge_subject["source_document_ids"][0], hygibot_id)
        self.assertEqual(result.document_ids_used[0], hygibot_id)
        self.assertNotEqual(result.document_ids_used[0], overview_id)



    def test_legacy_liro_subject_allows_new_warranty_document(self):
        self._seed_liro_with_new_technical_documents()

        result = self._retrieve_with_legacy_liro_subject(
            "Qual e a garantia do robo LIRO, da bateria e do carregador?"
        )

        self.assertIn(self.manifests["liro_littlebot"].id, result.document_ids_considered)
        self.assertEqual(result.chunks[0].document_id, self.manifests["liro_ficha_tecnica"].id)
        self.assertIn(self.manifests["liro_ficha_tecnica"].id, result.document_ids_used)
        self.assertIn("12 meses", result.context_text)
        self.assertIn("6 meses", result.context_text)
        self.assertNotEqual(result.document_ids_used, (self.manifests["liro_littlebot"].id,))

    def test_legacy_liro_subject_allows_new_battery_document(self):
        self._seed_liro_with_new_technical_documents()

        result = self._retrieve_with_legacy_liro_subject(
            "Qual e a capacidade da bateria e a autonomia estimada do LIRO?"
        )

        self.assertEqual(result.chunks[0].document_id, self.manifests["liro_ficha_tecnica"].id)
        self.assertIn(self.manifests["liro_ficha_tecnica"].id, result.document_ids_used)
        self.assertIn("6400 mAh", result.context_text)
        self.assertIn("ate 8 horas", result.context_text)

    def test_legacy_liro_subject_allows_new_network_document(self):
        self._seed_liro_with_new_technical_documents()

        result = self._retrieve_with_legacy_liro_subject("O LIRO funciona em Wi-Fi 5 GHz?")

        self.assertEqual(result.chunks[0].document_id, self.manifests["liro_requisitos_rede"].id)
        self.assertIn(self.manifests["liro_requisitos_rede"].id, result.document_ids_used)
        self.assertIn("5 GHz", result.context_text)
        self.assertIn("nao sao suportadas", result.context_text)

    def test_short_followup_uses_liro_subject_without_stale_document_whitelist(self):
        self._seed_liro_with_new_technical_documents()

        result = self._retrieve_with_legacy_liro_subject(
            "e a garantia?",
            contextual_query="LIRO / Little Bot garantia bateria carregador",
        )

        self.assertEqual(result.chunks[0].document_id, self.manifests["liro_ficha_tecnica"].id)
        self.assertIn(self.manifests["liro_ficha_tecnica"].id, result.document_ids_used)
        self.assertIn("12 meses", result.context_text)
        self.assertNotEqual(result.document_ids_used, (self.manifests["liro_littlebot"].id,))

    def test_generic_liro_question_can_still_use_commercial_summary_first(self):
        self._seed_liro_with_new_technical_documents()

        result = self._retrieve_with_legacy_liro_subject("Me fale sobre o LIRO")

        self.assertEqual(result.chunks[0].document_id, self.manifests["liro_littlebot"].id)
        self.assertIn("experiências educacionais", result.context_text)

    def test_legacy_liro_subject_without_direct_evidence_does_not_force_specs(self):
        self._seed_liro_with_new_technical_documents()

        result = self._retrieve_with_legacy_liro_subject("O LIRO possui certificacao IP67?")
        reply = synthesize_deterministic_reply(
            result.context_text,
            current_message="O LIRO possui certificacao IP67?",
            active_domain="robotics",
            active_application="educational_robotics",
        )

        self.assertNotIn("IP67", result.context_text)
        self.assertIn("não encontrei", reply.lower())

    def test_legacy_liro_subject_keeps_cross_product_documents_out(self):
        self._seed_liro_with_new_technical_documents()

        result = self._retrieve_with_legacy_liro_subject(
            "Qual e a capacidade da bateria e a autonomia estimada do LIRO?"
        )

        self.assertIn(self.manifests["liro_ficha_tecnica"].id, result.document_ids_used)
        self.assertNotIn(self.manifests["hygibot_dune"].id, result.document_ids_used)
        self.assertNotIn("HygiBot", result.context_text)
        self.assertNotIn("Dune Bot", result.context_text)

    def test_pronoun_followup_keeps_hygibot_subject(self):
        self._seed_xyron_family()
        self._turn("preciso de um robo de limpeza")
        self._turn("um galpão")
        self._turn("3000 m2, piso de concreto")
        reply, result = self._turn("ele consegue trabalhar com pessoas circulando?")
        self.assertIn("HygiBot", self.memory.active_knowledge_subject.get("canonical_name", ""))
        self.assertIn(
            self.manifests["hygibot_dune"].id,
            self.memory.active_knowledge_subject.get("source_document_ids", []),
        )
        lowered = reply.lower()
        self.assertTrue(
            any(token in lowered for token in ("fluxo", "pessoas", "circul", "confirmação", "documentação", "documentacao"))
            or "?" not in lowered,
            reply,
        )
