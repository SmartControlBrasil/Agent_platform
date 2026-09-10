from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from django.conf import settings


class PdfEmptyTextCause(str, Enum):
    NO_TEXT_LAYER = "no_text_layer"
    ENCRYPTED = "encrypted"
    CORRUPT = "corrupt"
    EXTRACTION_FAILED = "extraction_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PdfExtractionResult:
    text_bytes: bytes
    method: str
    empty_cause: str = ""
    page_count: int | None = None
    ocr_pages: int = 0


def classify_pdf_empty_text(*, raw: bytes, pdftotext_error: str = "") -> PdfEmptyTextCause:
    error = str(pdftotext_error or "").lower()
    if "password" in error or "encrypted" in error:
        return PdfEmptyTextCause.ENCRYPTED
    if "invalid" in error or "corrupt" in error or "damaged" in error:
        return PdfEmptyTextCause.CORRUPT
    if "could not extract" in error or "pdftotext" in error:
        return PdfEmptyTextCause.EXTRACTION_FAILED

    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo:
        with tempfile.TemporaryDirectory(prefix="livia-pdfinfo-") as tmp:
            pdf_path = Path(tmp) / "document.pdf"
            pdf_path.write_bytes(raw)
            try:
                completed = subprocess.run(
                    [pdfinfo, str(pdf_path)],
                    check=True,
                    capture_output=True,
                    timeout=15,
                    text=True,
                )
                info = completed.stdout.lower()
                if "encrypted" in info and "yes" in info:
                    return PdfEmptyTextCause.ENCRYPTED
            except Exception:
                pass

    # pdftotext returned empty without explicit error — likely scan/image-only PDF.
    return PdfEmptyTextCause.NO_TEXT_LAYER


def extract_pdf_text_pdftotext(raw: bytes) -> tuple[bytes, str]:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        raise RuntimeError("pdftotext is required to extract text from PDF files.")
    timeout = int(getattr(settings, "LIVIA_RAG_PDF_TEXT_TIMEOUT_SECONDS", 60) or 60)
    with tempfile.TemporaryDirectory(prefix="livia-pdf-") as tmp:
        pdf_path = Path(tmp) / "document.pdf"
        pdf_path.write_bytes(raw)
        try:
            completed = subprocess.run(
                [pdftotext, "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
                check=True,
                capture_output=True,
                timeout=max(timeout, 10),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("PDF text extraction timed out.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="ignore")
            raise RuntimeError(stderr or "Could not extract text from PDF file.") from exc
    text = completed.stdout.decode("utf-8", errors="ignore").strip()
    return text.encode("utf-8"), text


def extract_pdf_text_ocr(raw: bytes) -> tuple[bytes, int]:
    """
    OCR fallback for image-only PDFs.
    Requires poppler (pdftoppm) and tesseract installed on the host.
    """
    pdftoppm = shutil.which("pdftoppm")
    tesseract = shutil.which("tesseract")
    if not pdftoppm or not tesseract:
        raise RuntimeError(
            "PDF OCR fallback requires pdftoppm (poppler-utils) and tesseract installed on the host."
        )

    max_pages = int(getattr(settings, "LIVIA_RAG_PDF_OCR_MAX_PAGES", 600) or 600)
    total_timeout = int(getattr(settings, "LIVIA_RAG_PDF_OCR_TIMEOUT_SECONDS", 300) or 300)
    page_timeout = int(getattr(settings, "LIVIA_RAG_PDF_OCR_PAGE_TIMEOUT_SECONDS", 30) or 30)

    with tempfile.TemporaryDirectory(prefix="livia-pdf-ocr-") as tmp:
        tmp_path = Path(tmp)
        pdf_path = tmp_path / "document.pdf"
        pdf_path.write_bytes(raw)
        prefix = str(tmp_path / "page")
        try:
            subprocess.run(
                [pdftoppm, "-png", "-f", "1", "-l", str(max_pages), str(pdf_path), prefix],
                check=True,
                capture_output=True,
                timeout=total_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("PDF OCR conversion timed out.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="ignore")
            raise RuntimeError(stderr or "PDF OCR conversion failed.") from exc

        page_images = sorted(tmp_path.glob("page-*.png"))
        if not page_images:
            raise RuntimeError("PDF OCR produced no page images.")

        texts: list[str] = []
        for index, image_path in enumerate(page_images, start=1):
            try:
                completed = subprocess.run(
                    [tesseract, str(image_path), "stdout", "-l", "por+eng"],
                    check=True,
                    capture_output=True,
                    timeout=max(page_timeout, 5),
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"PDF OCR timed out on page {index}.") from exc
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b"").decode("utf-8", errors="ignore")
                raise RuntimeError(stderr or f"PDF OCR failed on page {index}.") from exc
            page_text = completed.stdout.decode("utf-8", errors="ignore").strip()
            if page_text:
                texts.append(page_text)

        if not texts:
            raise RuntimeError("PDF OCR returned empty content.")
        return "\n\n".join(texts).encode("utf-8"), len(page_images)


def extract_pdf_text_with_fallback(raw: bytes) -> PdfExtractionResult:
    """pdftotext first; OCR only when pdftotext returns empty."""
    pdftotext_error = ""
    try:
        payload, plain = extract_pdf_text_pdftotext(raw)
    except RuntimeError as exc:
        pdftotext_error = str(exc)
        payload = b""
        plain = ""

    if plain.strip():
        return PdfExtractionResult(text_bytes=payload, method="pdftotext")

    empty_cause = classify_pdf_empty_text(raw=raw, pdftotext_error=pdftotext_error)
    ocr_enabled = bool(getattr(settings, "LIVIA_RAG_PDF_OCR_ENABLED", True))
    if not ocr_enabled:
        raise RuntimeError(
            f"PDF text extraction returned empty content ({empty_cause.value}). OCR fallback is disabled."
        )

    try:
        ocr_payload, ocr_pages = extract_pdf_text_ocr(raw)
    except RuntimeError as exc:
        raise RuntimeError(
            f"PDF text extraction returned empty content ({empty_cause.value}). OCR fallback failed: {exc}"
        ) from exc

    return PdfExtractionResult(
        text_bytes=ocr_payload,
        method="ocr_tesseract",
        empty_cause=empty_cause.value,
        ocr_pages=ocr_pages,
    )
