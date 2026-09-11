from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from knowledge_base.models import TenantRagDriveFileManifest
from knowledge_base.rag.chunking import (
    RagChunkingError,
    build_deterministic_chunks,
    estimate_chunk_count,
    iter_deterministic_chunks,
    load_chunk_config,
)
from knowledge_base.rag.google_drive_inventory import (
    GoogleDriveInventoryService,
    normalize_text_for_rag,
    resolve_drive_export_mime,
)
from knowledge_base.rag.html_extraction import extract_html_for_rag
from knowledge_base.rag.ingestion_accounting import (
    is_office_temporary_artifact,
    summarize_manifest_accounting,
)
from knowledge_base.rag.pdf_extraction import (
    PdfEmptyTextCause,
    PdfExtractionResult,
    classify_pdf_empty_text,
    extract_pdf_text_with_fallback,
)
from knowledge_base.rag.sync import SUPPORTED_EXPORT_MIME_TYPES


class LongDocumentChunkingTests(SimpleTestCase):
    @override_settings(
        LIVIA_RAG_CHUNK_SIZE_CHARS=1200,
        LIVIA_RAG_CHUNK_OVERLAP_CHARS=120,
        LIVIA_RAG_MAX_CHUNKS_PER_DOCUMENT=2500,
        LIVIA_RAG_CHUNK_GUARD_MAX_ITERATIONS=50000,
    )
    def test_document_with_more_than_400_chunks_succeeds(self):
        config = load_chunk_config()
        paragraph = ("Parágrafo acadêmico com conteúdo suficiente. " * 20).strip()
        text = "\n\n".join(paragraph for _ in range(900))
        chunks = build_deterministic_chunks(text, config)
        self.assertGreater(len(chunks), 400)
        self.assertLessEqual(len(chunks), config.max_chunks_per_document)

    @override_settings(
        LIVIA_RAG_CHUNK_SIZE_CHARS=1200,
        LIVIA_RAG_CHUNK_OVERLAP_CHARS=120,
        LIVIA_RAG_MAX_CHUNKS_PER_DOCUMENT=2500,
        LIVIA_RAG_CHUNK_GUARD_MAX_ITERATIONS=50000,
    )
    def test_large_representative_document_preserves_order_overlap_and_hashes(self):
        config = load_chunk_config()
        text = "\n\n".join(f"Seção {index}. " + ("conteúdo " * 400) for index in range(150))
        chunks_a = build_deterministic_chunks(text, config)
        chunks_b = build_deterministic_chunks(text, config)
        self.assertEqual(chunks_a, chunks_b)
        self.assertGreater(len(chunks_a), 400)
        ordinals = [chunk.ordinal for chunk in chunks_a]
        self.assertEqual(ordinals, list(range(len(chunks_a))))
        starts = [chunk.start_char for chunk in chunks_a]
        self.assertEqual(starts, sorted(starts))
        for left, right in zip(chunks_a, chunks_a[1:]):
            overlap = left.end_char - right.start_char
            self.assertGreaterEqual(overlap, 0)
            self.assertLessEqual(overlap, config.overlap_chars + 5)

    @override_settings(
        LIVIA_RAG_CHUNK_SIZE_CHARS=200,
        LIVIA_RAG_CHUNK_OVERLAP_CHARS=20,
        LIVIA_RAG_MAX_CHUNKS_PER_DOCUMENT=12,
        LIVIA_RAG_CHUNK_GUARD_MAX_ITERATIONS=50000,
    )
    def test_guardrail_error_reports_required_chunk_count(self):
        config = load_chunk_config()
        text = "Bloco. " * 4000
        required = estimate_chunk_count(len(text), config)
        with self.assertRaises(RagChunkingError) as ctx:
            build_deterministic_chunks(text, config)
        message = str(ctx.exception)
        self.assertIn("requires", message)
        self.assertIn(str(config.max_chunks_per_document), message)
        self.assertGreater(required, config.max_chunks_per_document)

    @override_settings(
        LIVIA_RAG_CHUNK_SIZE_CHARS=80,
        LIVIA_RAG_CHUNK_OVERLAP_CHARS=8,
        LIVIA_RAG_MAX_CHUNKS_PER_DOCUMENT=500,
        LIVIA_RAG_CHUNK_GUARD_MAX_ITERATIONS=50000,
    )
    def test_iter_and_build_produce_same_chunks(self):
        config = load_chunk_config()
        text = ("Capítulo.\n\n" + ("Linha de texto. " * 20) + "\n\n") * 20
        self.assertEqual(
            build_deterministic_chunks(text, config),
            list(iter_deterministic_chunks(text, config)),
        )


class HtmlExtractionTests(SimpleTestCase):
    def test_extract_html_for_rag_collects_title_text_links_and_video_refs(self):
        raw = b"""<!doctype html>
        <html><head><title>Video Aulas Anhembi Morumbi</title></head>
        <body>
          <h1>Portal de aulas</h1>
          <p>Confira as disciplinas disponiveis.</p>
          <a href="https://www.youtube.com/watch?v=abc123">Aula 1</a>
          <iframe src="https://player.vimeo.com/video/999"></iframe>
        </body></html>"""
        text, meta = extract_html_for_rag(raw)
        self.assertIn("Video Aulas Anhembi Morumbi", text)
        self.assertIn("Portal de aulas", text)
        self.assertIn("https://www.youtube.com/watch?v=abc123", text)
        self.assertGreaterEqual(meta["link_count"], 1)
        self.assertGreaterEqual(meta["video_reference_count"], 1)

    def test_html_is_supported_mime_after_extractor_exists(self):
        self.assertIn("text/html", SUPPORTED_EXPORT_MIME_TYPES)


class MarkdownIngestionTests(SimpleTestCase):
    def test_markdown_mime_is_supported(self):
        self.assertIn("text/markdown", SUPPORTED_EXPORT_MIME_TYPES)

    def test_resolve_drive_export_mime_accepts_md_extension_with_text_plain(self):
        resolved = resolve_drive_export_mime(
            mime_type="text/plain",
            filename="Controle e Automacao/unidade-1.md",
            supported_mimes=SUPPORTED_EXPORT_MIME_TYPES,
        )
        self.assertEqual(resolved, "text/plain")

    def test_resolve_drive_export_mime_maps_unknown_mime_with_md_extension(self):
        resolved = resolve_drive_export_mime(
            mime_type="application/octet-stream",
            filename="Gestao/modulo-1.md",
            supported_mimes=SUPPORTED_EXPORT_MIME_TYPES,
        )
        self.assertEqual(resolved, "text/markdown")

    def test_normalize_preserves_markdown_headings(self):
        raw = "# Controle e Automacao\n\n## Redes Industriais\n\nConteudo util."
        normalized = normalize_text_for_rag(raw)
        self.assertIn("# Controle e Automacao", normalized)
        self.assertIn("## Redes Industriais", normalized)

    @override_settings(
        LIVIA_RAG_CHUNK_SIZE_CHARS=200,
        LIVIA_RAG_CHUNK_OVERLAP_CHARS=20,
        LIVIA_RAG_MAX_CHUNKS_PER_DOCUMENT=500,
        LIVIA_RAG_CHUNK_GUARD_MAX_ITERATIONS=50000,
    )
    def test_markdown_chunks_keep_heading_context(self):
        config = load_chunk_config()
        text = normalize_text_for_rag("# Manutencao\n\nTexto sobre TPM e confiabilidade.")
        chunks = build_deterministic_chunks(text, config)
        self.assertGreaterEqual(len(chunks), 1)
        joined = " ".join(chunk.text for chunk in chunks)
        self.assertIn("# Manutencao", joined)

    def test_export_file_text_markdown_records_metadata(self):
        service = GoogleDriveInventoryService(service=object())
        payload = b"# Inteligencia Artificial\n\nConteudo sobre ML."
        with patch.object(service, "service") as drive:
            drive.files.return_value.get_media.return_value.execute.return_value = payload
            exported = service.export_file_text(
                "file-md",
                "text/markdown",
                filename="IA/introducao.md",
            )
        self.assertIn(b"# Inteligencia Artificial", exported)
        self.assertEqual(service.last_extraction_metadata["extraction_method"], "markdown_utf8")


class TemporaryArtifactTests(SimpleTestCase):
    def test_detects_office_lock_files(self):
        self.assertTrue(is_office_temporary_artifact("~$pa de Estudo TPM.docx"))
        self.assertTrue(is_office_temporary_artifact("~$Mapa.xlsx"))
        self.assertFalse(is_office_temporary_artifact("Mapa de Estudo TPM.docx"))


class PdfExtractionFallbackTests(SimpleTestCase):
    def test_classifies_empty_pdf_as_no_text_layer(self):
        cause = classify_pdf_empty_text(raw=b"%PDF-1.4", pdftotext_error="")
        self.assertEqual(cause, PdfEmptyTextCause.NO_TEXT_LAYER)

    @patch("knowledge_base.rag.pdf_extraction.extract_pdf_text_pdftotext", return_value=(b"", ""))
    @patch(
        "knowledge_base.rag.pdf_extraction.extract_pdf_text_ocr",
        return_value=(b"texto ocr", 2),
    )
    def test_empty_pdftotext_triggers_ocr_fallback(self, _ocr, _pdftotext):
        result = extract_pdf_text_with_fallback(b"%PDF-1.4")
        self.assertEqual(result.method, "ocr_tesseract")
        self.assertEqual(result.text_bytes, b"texto ocr")
        self.assertEqual(result.ocr_pages, 2)

    @patch("knowledge_base.rag.pdf_extraction.extract_pdf_text_pdftotext", return_value=(b"texto", "texto"))
    @patch("knowledge_base.rag.pdf_extraction.extract_pdf_text_ocr")
    def test_pdftotext_with_text_does_not_call_ocr(self, ocr_mock, _pdftotext):
        result = extract_pdf_text_with_fallback(b"%PDF-1.4")
        self.assertEqual(result.method, "pdftotext")
        ocr_mock.assert_not_called()


class GoogleDriveExportIntegrationTests(SimpleTestCase):
    @patch("knowledge_base.rag.google_drive_inventory.extract_pdf_text_with_fallback")
    def test_export_file_text_records_pdf_extraction_metadata(self, pdf_mock):
        pdf_mock.return_value = PdfExtractionResult(
            text_bytes=b"conteudo pdf",
            method="pdftotext",
        )
        service = GoogleDriveInventoryService(service=object())
        with patch.object(service, "service") as drive:
            drive.files.return_value.get_media.return_value.execute.return_value = b"%PDF"
            payload = service.export_file_text("file-1", "application/pdf")
        self.assertEqual(payload, b"conteudo pdf")
        self.assertEqual(service.last_extraction_metadata["extraction_method"], "pdftotext")

    def test_export_file_text_extracts_html_without_external_crawl(self):
        service = GoogleDriveInventoryService(service=object())
        html = b"<html><head><title>Aulas</title></head><body><p>Link</p><a href='https://youtu.be/abc'>v</a></body></html>"
        with patch.object(service, "service") as drive:
            drive.files.return_value.get_media.return_value.execute.return_value = html
            payload = service.export_file_text("file-html", "text/html")
        decoded = payload.decode("utf-8")
        self.assertIn("Aulas", decoded)
        self.assertIn("https://youtu.be/abc", decoded)
        self.assertEqual(service.last_extraction_metadata["extraction_method"], "html_parser")


class IngestionAccountingTests(TestCase):
    def test_accounting_equation_balances(self):
        from tenants.models import Tenant
        from knowledge_base.models import TenantRagConfiguration

        tenant = Tenant.objects.create(name="Contagem", slug="contagem")
        configuration = TenantRagConfiguration.objects.create(tenant=tenant, source_mode="manual")
        manifests = [
            TenantRagDriveFileManifest.objects.create(
                tenant=tenant,
                configuration=configuration,
                drive_file_id="ok-1",
                name="ok.pdf",
                mime_type="application/pdf",
                status=TenantRagDriveFileManifest.Status.EXPORTED,
            ),
            TenantRagDriveFileManifest.objects.create(
                tenant=tenant,
                configuration=configuration,
                drive_file_id="tmp-1",
                name="~$lock.docx",
                mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                status=TenantRagDriveFileManifest.Status.SKIPPED_TEMPORARY_ARTIFACT,
            ),
            TenantRagDriveFileManifest.objects.create(
                tenant=tenant,
                configuration=configuration,
                drive_file_id="bad-1",
                name="broken.pdf",
                mime_type="application/pdf",
                status=TenantRagDriveFileManifest.Status.FAILED,
            ),
        ]
        summary = summarize_manifest_accounting(manifests)
        self.assertEqual(summary["DISCOVERED_TOTAL"], 3)
        self.assertEqual(summary["INGESTED"], 1)
        self.assertEqual(summary["SKIPPED_TEMPORARY"], 1)
        self.assertEqual(summary["FAILED"], 1)
        self.assertTrue(summary["EQUATION_BALANCED"])
