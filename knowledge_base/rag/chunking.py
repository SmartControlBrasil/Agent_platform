from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Iterator

from django.conf import settings

class RagChunkingError(Exception):
    pass


@dataclass(frozen=True)
class ChunkConfig:
    chunk_size_chars: int
    overlap_chars: int
    max_chunks_per_document: int
    guard_max_iterations: int

    @property
    def signature(self) -> str:
        raw = (
            f"{self.chunk_size_chars}:{self.overlap_chars}:"
            f"{self.max_chunks_per_document}:{self.guard_max_iterations}"
        )
        return sha256(raw.encode("utf-8")).hexdigest()

    @property
    def effective_stride(self) -> int:
        return max(self.chunk_size_chars - self.overlap_chars, 1)


@dataclass(frozen=True)
class ChunkRecord:
    ordinal: int
    text: str
    chunk_sha256: str
    start_char: int
    end_char: int
    char_count: int
    byte_count: int


def load_chunk_config() -> ChunkConfig:
    size = int(getattr(settings, "LIVIA_RAG_CHUNK_SIZE_CHARS", 1200) or 0)
    overlap = int(getattr(settings, "LIVIA_RAG_CHUNK_OVERLAP_CHARS", 120) or 0)
    max_chunks = int(getattr(settings, "LIVIA_RAG_MAX_CHUNKS_PER_DOCUMENT", 2500) or 0)
    guard_iterations = int(getattr(settings, "LIVIA_RAG_CHUNK_GUARD_MAX_ITERATIONS", 50000) or 0)
    if size <= 0:
        raise RagChunkingError("LIVIA_RAG_CHUNK_SIZE_CHARS must be a positive integer.")
    if overlap < 0:
        raise RagChunkingError("LIVIA_RAG_CHUNK_OVERLAP_CHARS must be zero or positive.")
    if overlap >= size:
        raise RagChunkingError("LIVIA_RAG_CHUNK_OVERLAP_CHARS must be smaller than LIVIA_RAG_CHUNK_SIZE_CHARS.")
    if max_chunks <= 0:
        raise RagChunkingError("LIVIA_RAG_MAX_CHUNKS_PER_DOCUMENT must be a positive integer.")
    if guard_iterations <= 0:
        raise RagChunkingError("LIVIA_RAG_CHUNK_GUARD_MAX_ITERATIONS must be a positive integer.")
    return ChunkConfig(
        chunk_size_chars=size,
        overlap_chars=overlap,
        max_chunks_per_document=max_chunks,
        guard_max_iterations=guard_iterations,
    )


def estimate_chunk_count(text_length: int, config: ChunkConfig) -> int:
    if text_length <= 0:
        return 0
    if text_length <= config.chunk_size_chars:
        return 1
    return int((text_length + config.effective_stride - 1) // config.effective_stride)


def iter_deterministic_chunks(text: str, config: ChunkConfig) -> Iterator[ChunkRecord]:
    """Yield chunks iteratively to avoid unnecessary intermediate structures."""
    normalized = str(text or "")
    if not normalized.strip():
        return
    if len(normalized) <= config.chunk_size_chars:
        yield _make_record(0, normalized, 0, len(normalized))
        return

    boundaries = _candidate_boundaries(normalized)
    start = 0
    total = len(normalized)
    guard = 0
    ordinal = 0
    while start < total:
        guard += 1
        if guard > config.guard_max_iterations:
            raise RagChunkingError("Chunking loop guard triggered due to invalid overlap progression.")
        preferred_end = min(start + config.chunk_size_chars, total)
        end = _pick_best_end(
            boundaries,
            start,
            preferred_end,
            total,
            min_span=config.effective_stride,
        )
        if end <= start:
            end = min(start + config.chunk_size_chars, total)
        chunk_text = normalized[start:end].strip()
        if chunk_text:
            if ordinal >= config.max_chunks_per_document:
                required = estimate_chunk_count(total, config)
                raise RagChunkingError(
                    "Document requires "
                    f"{required} chunks but LIVIA_RAG_MAX_CHUNKS_PER_DOCUMENT is "
                    f"{config.max_chunks_per_document}."
                )
            yield _make_record(ordinal, chunk_text, start, end)
            ordinal += 1
        if end >= total:
            break
        next_start = end - config.overlap_chars
        if next_start <= start:
            next_start = start + config.effective_stride
        start = min(next_start, total)


def build_deterministic_chunks(text: str, config: ChunkConfig) -> list[ChunkRecord]:
    chunks = list(iter_deterministic_chunks(text, config))
    if chunks and any(not item.text for item in chunks):
        raise RagChunkingError("Chunking generated empty chunks, aborting.")
    return chunks


def _candidate_boundaries(text: str) -> list[int]:
    boundaries = {0, len(text)}
    for match in re.finditer(r"\n\n+", text):
        boundaries.add(match.start() + 1)
    for match in re.finditer(r"(?<=[\.\!\?\;\:])\s+", text):
        boundaries.add(match.end())
    return sorted(boundaries)


def _pick_best_end(
    boundaries: list[int],
    start: int,
    preferred_end: int,
    total: int,
    *,
    min_span: int,
) -> int:
    minimum_end = min(start + max(min_span, 1), total)
    inside = [point for point in boundaries if minimum_end <= point <= preferred_end]
    if inside:
        return max(inside)
    fallback = [point for point in boundaries if point > preferred_end]
    if fallback:
        candidate = min(fallback)
        if candidate - start <= int(preferred_end - start) * 2:
            return candidate
    return min(preferred_end, total)


def _make_record(ordinal: int, text: str, start: int, end: int) -> ChunkRecord:
    chunk_hash = sha256(text.encode("utf-8")).hexdigest()
    return ChunkRecord(
        ordinal=ordinal,
        text=text,
        chunk_sha256=chunk_hash,
        start_char=max(start, 0),
        end_char=max(end, 0),
        char_count=len(text),
        byte_count=len(text.encode("utf-8")),
    )
