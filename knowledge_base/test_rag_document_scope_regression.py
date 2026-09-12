from django.test import SimpleTestCase

from knowledge_base.rag.entity_catalog import extract_document_metadata


class RagDocumentScopeRegressionTests(SimpleTestCase):

    def test_academic_unit_is_general_not_catalog(self):
        metadata = extract_document_metadata(
            file_name="Unidade 1.md",
            relative_path=(
                "Engenharia de Controle e Automação/"
                "Automação Industrial/Unidade 1.md"
            ),
            text="""
# Unidade 1
## Página 1
AUTOMAÇÃO INDUSTRIAL
INTRODUÇÃO AO SISTEMA DE PRODUÇÃO
## Página 2
E-Book Apostila
""",
        )

        self.assertEqual(metadata["document_scope"], "general")
        self.assertNotIn("Página 1", metadata["product_names"])
        self.assertNotIn("Unidade 1", metadata["product_names"])

    def test_explicit_product_overview_remains_catalog_overview(self):
        metadata = extract_document_metadata(
            file_name="robotica_xyron_visao_geral.md",
            relative_path="01_XYRON/robotica_xyron_visao_geral.md",
            text="""
# Robótica de serviço Xyron

## Produtos oficiais no site
- LIRO / Little Bot
- HygiBot / Dune Bot
- Orbit Bot

## Orientação rápida
""",
        )

        self.assertEqual(metadata["document_scope"], "catalog_overview")

    def test_explicit_official_product_is_product_dedicated(self):
        metadata = extract_document_metadata(
            file_name="hygibot_dune.md",
            relative_path="01_XYRON/hygibot_dune.md",
            text="""
# HygiBot / Dune Bot — robô de limpeza autônoma

Nome oficial: HygiBot / Dune Bot
Categoria: limpeza profissional
""",
        )

        self.assertEqual(metadata["document_scope"], "product_dedicated")

    def test_strong_model_identifier_is_product_dedicated(self):
        metadata = extract_document_metadata(
            file_name="NEXUS R7 Manual.md",
            relative_path="produtos/NEXUS R7 Manual.md",
            text="""
# NEXUS R7

Robô móvel para inspeção interna.
Autonomia nominal: 7 horas.
""",
        )

        self.assertEqual(metadata["document_scope"], "product_dedicated")

    def test_academic_overview_phrase_does_not_mean_catalog(self):
        metadata = extract_document_metadata(
            file_name="Unidade 4.md",
            relative_path=(
                "Engenharia de Controle e Automação/"
                "Controle de Sistemas/Unidade 4.md"
            ),
            text="""
# Unidade 4
CONTROLE DE SISTEMAS
Visão Geral — Controle Digital
Transformada Z e sistemas discretos.
""",
        )

        self.assertEqual(metadata["document_scope"], "general")

    def test_academic_alphanumeric_term_is_not_product_model(self):
        metadata = extract_document_metadata(
            file_name="Seq2Seq e Reconhecimento de Voz.md",
            relative_path=(
                "Inteligenicia artificial/"
                "PROCESSAMENTO DE LINGUAGEM NATURAL/"
                "Seq2Seq e Reconhecimento de Voz.md"
            ),
            text="""
# Seq2Seq e Reconhecimento de Voz

Arquiteturas sequence-to-sequence são utilizadas em tarefas
de processamento de linguagem natural.
""",
        )

        self.assertEqual(metadata["document_scope"], "general")

    def test_academic_overview_phrase_does_not_mean_catalog(self):
        metadata = extract_document_metadata(
            file_name="Unidade 4.md",
            relative_path=(
                "Engenharia de Controle e Automação/"
                "Controle de Sistemas/Unidade 4.md"
            ),
            text="""
# Unidade 4
CONTROLE DE SISTEMAS
Visão Geral — Controle Digital
Transformada Z e sistemas discretos.
""",
        )

        self.assertEqual(metadata["document_scope"], "general")

    def test_academic_alphanumeric_term_is_not_product_model(self):
        metadata = extract_document_metadata(
            file_name="Seq2Seq e Reconhecimento de Voz.md",
            relative_path=(
                "Inteligenicia artificial/"
                "PROCESSAMENTO DE LINGUAGEM NATURAL/"
                "Seq2Seq e Reconhecimento de Voz.md"
            ),
            text="""
# Seq2Seq e Reconhecimento de Voz

Arquiteturas sequence-to-sequence são utilizadas em tarefas
de processamento de linguagem natural.
""",
        )

        self.assertEqual(metadata["document_scope"], "general")
