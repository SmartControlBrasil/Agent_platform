from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from knowledge_base.models import TenantRagDriveFileManifest

ENTITY_TYPE_PRODUCT = "product"

_GENERIC_TITLE_WORDS = {
    "manual", "catalogo", "catalog", "documento", "document", "ficha", "tecnica", "técnica",
    "especificacao", "especificação", "overview", "visao", "visão", "geral", "produto", "produtos",
}

_STOP_UPPER_PHRASES = {
    "fonte", "conteudo", "conteúdo", "tags", "observacoes", "observações", "atencao", "atenção",
}

_PRONOUN_OR_ELLIPTIC_RE = re.compile(
    r"\b(ele|ela|esse|essa|este|esta|desse|dessa|dele|dela|modelo|equipamento|robo|robô|produto)\b|^\s*e\b",
    flags=re.IGNORECASE,
)

_CATALOG_ORIENTATION_RE = re.compile(r"(?:→|->)")
_OFFICIAL_NAME_RE = re.compile(r"(?i)^nome oficial\s*:\s*(.+)$")
_PRODUCT_HEADING_RE = re.compile(r"^(.+?)\s*[—–-]\s*(?:rob[oô]|robótica|limpeza|segurança|patrulhamento|educacional)", re.IGNORECASE)

_GENERIC_CATEGORY_TOKENS = frozenset({
    "limpeza", "seguranca", "patrulha", "escola", "educacao", "educacional",
    "recepcao", "inspecao", "entrega", "assistencia", "corte", "grama",
})

_APPLICATION_ENTITY_HINTS: dict[str, tuple[str, ...]] = {
    "cleaning_robotics": ("hygibot", "dune", "duno", "limpeza"),
    "security_robotics": ("orbit", "patrol", "patrulha", "seguranca"),
    "educational_robotics": ("liro", "little bot", "littlebot", "educacional"),
}

_APPLICATION_QUERY_MARKERS: dict[str, tuple[str, ...]] = {
    "cleaning_robotics": ("limpeza", "lavar", "varrer", "aspirar", "hygibot", "dune", "duno", "galpao", "piso"),
    "security_robotics": ("seguranca", "patrulha", "orbit", "patrol", "vigilancia", "monitoramento"),
    "educational_robotics": ("escola", "educacional", "professor", "aluno", "bncc", "liro", "little bot"),
}

_DOCUMENT_SCOPE_SCORES = {
    "product_dedicated": 1.0,
    "general": 0.55,
    "catalog_overview": 0.15,
}


@dataclass(frozen=True)
class KnowledgeEntity:
    canonical_name: str
    aliases: tuple[str, ...] = ()
    entity_type: str = ENTITY_TYPE_PRODUCT
    document_ids: tuple[int, ...] = ()
    confidence: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_subject(self, *, match_method: str = "", confidence: float | None = None) -> dict:
        return {
            "canonical_name": self.canonical_name,
            "entity_type": self.entity_type,
            "source_document_ids": list(self.document_ids),
            "confidence": float(confidence if confidence is not None else self.confidence),
            "match_method": match_method or "",
        }


@dataclass(frozen=True)
class EntityResolution:
    subject: dict | None = None
    matches: tuple[KnowledgeEntity, ...] = ()
    ambiguous: bool = False
    ambiguity_options: tuple[str, ...] = ()
    confidence: float = 0.0
    method: str = "none"


def normalize_entity_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def extract_document_metadata(*, file_name: str, mime_type: str = "", relative_path: str = "", text: str = "", source_modified_time=None) -> dict:
    title = _document_title(file_name=file_name, relative_path=relative_path, text=text)
    doc_type = _document_type(file_name=file_name, mime_type=mime_type, text=text)
    candidates = _entity_candidates(file_name=file_name, title=title, text=text)
    product_names = _dedupe_preserve([c for c in candidates if _looks_like_product_name(c)])
    model_names = _dedupe_preserve([c for c in candidates if _looks_like_model_name(c)])
    if title and _looks_like_product_name(title):
        product_names = _dedupe_preserve([title, *product_names])
    heading_product = _product_name_from_heading(title)
    if heading_product:
        product_names = _dedupe_preserve([heading_product, *product_names])
    document_scope = _document_scope(
        file_name=file_name,
        relative_path=relative_path,
        text=text,
        product_names=product_names,
    )
    return {
        "source_document_id": "",
        "file_name": str(file_name or ""),
        "relative_path": str(relative_path or ""),
        "document_title": title or None,
        "document_type": doc_type or None,
        "document_scope": document_scope,
        "product_names": product_names,
        "model_names": model_names,
        "section": None,
        "heading": None,
        "page_number": None,
        "source_modified_time": source_modified_time.isoformat() if hasattr(source_modified_time, "isoformat") else None,
    }


def document_specificity_score(*, document_metadata: dict | None, file_name: str = "", text: str = "") -> float:
    meta = dict(document_metadata or {})
    scope = str(meta.get("document_scope") or _document_scope(
        file_name=file_name or meta.get("file_name") or "",
        relative_path="",
        text=text,
        product_names=list(meta.get("product_names") or []),
    ))
    base = float(_DOCUMENT_SCOPE_SCORES.get(scope, 0.5))
    product_count = len(meta.get("product_names") or [])
    if product_count == 1:
        base += 0.1
    elif product_count >= 5:
        base -= 0.2
    return max(0.0, min(1.0, base))


def build_chunk_metadata(*, document_metadata: dict | None, text: str, start_char: int = 0) -> dict:
    base = dict(document_metadata or {})
    heading = _nearest_markdown_heading(str(text or ""))
    base.update({
        "section": heading,
        "heading": heading,
        "page_number": _page_number_hint(str(text or "")),
        "relative_path": base.get("relative_path") or "",
        "source_document_id": base.get("source_document_id") or "",
    })
    return base


def entity_catalog_for_tenant(tenant) -> list[KnowledgeEntity]:
    if tenant is None:
        return []
    docs = TenantRagDriveFileManifest.objects.filter(
        tenant=tenant,
        is_active=True,
    ).exclude(
        status__in=["failed", "removed", "unavailable", "skipped_unsupported", "skipped_temporary_artifact"]
    ).order_by("id")
    grouped: dict[str, dict] = {}
    for manifest in docs:
        meta = dict(getattr(manifest, "document_metadata", None) or {})
        manifest_specificity = document_specificity_score(
            document_metadata=meta,
            file_name=str(getattr(manifest, "name", "") or ""),
        )
        names = [*list(meta.get("product_names") or []), *list(meta.get("model_names") or [])]
        title = meta.get("document_title") or manifest.name
        heading_product = _product_name_from_heading(str(title or ""))
        if heading_product:
            names.append(heading_product)
        if title and _looks_like_product_name(str(title)):
            names.append(str(title))
        for name in _dedupe_preserve(names):
            canonical = _canonicalize_name(name)
            if not canonical or _is_catalog_orientation_canonical(canonical):
                continue
            key = _entity_group_key(canonical, name)
            if not key:
                continue
            entry = grouped.setdefault(
                key,
                {
                    "canonical": canonical,
                    "aliases": set(),
                    "documents": set(),
                    "doc_specificity": {},
                    "confidence": 0.75,
                },
            )
            if manifest_specificity >= entry["doc_specificity"].get(manifest.id, 0.0):
                if manifest_specificity > max(entry["doc_specificity"].values() or [0.0]):
                    entry["canonical"] = canonical
            entry["documents"].add(manifest.id)
            entry["doc_specificity"][manifest.id] = manifest_specificity
            entry["aliases"].add(str(name).strip())
            for alias in meta.get("aliases") or []:
                if str(alias).strip():
                    entry["aliases"].add(str(alias).strip())
            for model in meta.get("model_names") or []:
                if str(model).strip():
                    entry["aliases"].add(str(model).strip())
    result = []
    for item in grouped.values():
        aliases = tuple(sorted({a for a in item["aliases"] if normalize_entity_text(a) != normalize_entity_text(item["canonical"])}))
        doc_ids = tuple(
            doc_id
            for doc_id, _score in sorted(
                item["doc_specificity"].items(),
                key=lambda pair: (-pair[1], pair[0]),
            )
        )
        result.append(KnowledgeEntity(
            canonical_name=item["canonical"],
            aliases=aliases,
            document_ids=doc_ids,
            confidence=float(item["confidence"]),
            metadata={"doc_specificity": dict(item["doc_specificity"])},
        ))
    return sorted(result, key=lambda e: normalize_entity_text(e.canonical_name))


def resolve_knowledge_entity(
    *,
    tenant,
    message: str,
    active_subject: dict | None = None,
    active_application: str = "",
    active_topic: str = "",
) -> EntityResolution:
    msg_n = normalize_entity_text(message)
    if not msg_n:
        return EntityResolution()
    catalog = entity_catalog_for_tenant(tenant)
    scored: list[tuple[KnowledgeEntity, float, str]] = []
    for entity in catalog:
        names = (entity.canonical_name, *entity.aliases)
        best_score = 0.0
        best_method = "none"
        for name in names:
            name_n = normalize_entity_text(name)
            if not name_n or _is_catalog_orientation_canonical(name):
                continue
            if _contains_phrase(msg_n, name_n):
                score = 1.0 if len(name_n.split()) > 1 else 0.86
                method = "exact"
            elif all(token in msg_n.split() for token in name_n.split()) and len(name_n.split()) > 1:
                score = 0.88
                method = "lexical"
            elif name_n in msg_n or msg_n in name_n:
                score = 0.62
                method = "partial"
            elif any(len(token) >= 4 and _contains_phrase(msg_n, token) for token in name_n.split()):
                if any(token in _GENERIC_CATEGORY_TOKENS for token in name_n.split()):
                    score = 0.0
                    method = "none"
                else:
                    score = 0.62
                    method = "partial"
            else:
                continue
            if score > best_score:
                best_score = score
                best_method = method
        family_score, family_method = _score_application_family_entity(
            entity,
            active_application=active_application,
            active_topic=active_topic,
            msg_n=msg_n,
        )
        if family_score > best_score:
            best_score = family_score
            best_method = family_method
        if best_score:
            scored.append((entity, best_score, best_method))
    scored.sort(
        key=lambda item: (
            -item[1],
            -_entity_specificity(item[0]),
            -len(normalize_entity_text(item[0].canonical_name).split()),
            item[0].canonical_name,
        )
    )
    if scored:
        top_score = scored[0][1]
        top = [item for item in scored if abs(item[1] - top_score) < 0.04]
        if len(top) > 1 and top_score < 0.9:
            top.sort(key=lambda item: (-_entity_specificity(item[0]), item[0].canonical_name))
            if len(top) > 1 and abs(_entity_specificity(top[0][0]) - _entity_specificity(top[1][0])) >= 0.15:
                entity, score, method = top[0]
                return EntityResolution(
                    subject=entity.to_subject(match_method=method, confidence=score),
                    matches=(entity,),
                    confidence=score,
                    method=method,
                )
            options = tuple(entity.canonical_name for entity, _score, _method in top[:5])
            return EntityResolution(matches=tuple(entity for entity, _score, _method in top), ambiguous=True, ambiguity_options=options, confidence=top_score, method="ambiguous")
        entity, score, method = scored[0]
        return EntityResolution(
            subject=entity.to_subject(match_method=method, confidence=score),
            matches=(entity,),
            confidence=score,
            method=method,
        )
    if active_subject and _PRONOUN_OR_ELLIPTIC_RE.search(str(message or "")):
        confidence = float(active_subject.get("confidence") or 0.0)
        if confidence >= 0.55:
            return EntityResolution(subject=dict(active_subject), confidence=confidence, method="active_subject")
    return EntityResolution()


def _document_title(*, file_name: str, relative_path: str, text: str) -> str:
    for line in str(text or "").splitlines()[:20]:
        stripped = line.strip().strip("# ").strip()
        if stripped and len(stripped) <= 120 and not stripped.lower().startswith(("fonte:", "tags:", "conteúdo:", "conteudo:")):
            heading_product = _product_name_from_heading(stripped)
            if heading_product:
                return heading_product
            return _clean_title(stripped)
    stem = Path(str(file_name or relative_path or "")).stem.replace("_", " ").replace("-", " ").strip()
    return _clean_title(stem)


def _document_type(*, file_name: str, mime_type: str, text: str) -> str:
    blob = normalize_entity_text(f"{file_name} {mime_type} {str(text or '')[:500]}")
    if "manual" in blob:
        return "manual"
    if "catalogo" in blob or "catalog" in blob:
        return "catalog"
    if "ficha tecnica" in blob or "especificacao" in blob:
        return "technical_sheet"
    if "markdown" in blob or str(file_name).lower().endswith(".md"):
        return "markdown"
    if "pdf" in blob:
        return "pdf"
    return "document"


def _entity_candidates(*, file_name: str, title: str, text: str) -> list[str]:
    sample_lines = [title, Path(str(file_name or "")).stem.replace("_", " ").replace("-", " ")]
    sample_lines.extend(line.strip().strip("# ") for line in str(text or "").splitlines()[:40])
    candidates: list[str] = []
    for line in sample_lines:
        official = _parse_official_name_line(line)
        if official:
            candidates.append(official)
        if _is_catalog_orientation_line(line):
            product = _parse_catalog_orientation_product(line)
            if product:
                candidates.append(product)
            continue
        clean = _clean_title(line)
        if clean and _looks_like_product_name(clean):
            candidates.append(clean)
        candidates.extend(match.group(0).strip() for match in re.finditer(r"\b[A-Z][A-Z0-9]{1,}(?:[ -][A-Z0-9]{1,}){0,3}\b", line))
        candidates.extend(match.group(0).strip() for match in re.finditer(r"\b[A-Za-z]{2,}[ -]?[A-Z]{0,3}\d{1,4}[A-Z0-9-]*\b", line))
    return _dedupe_preserve(_clean_title(c) for c in candidates if c)


def _looks_like_product_name(value: str) -> bool:
    n = normalize_entity_text(value)
    raw = str(value or "").strip()
    if not n or n in _GENERIC_TITLE_WORDS or n in _STOP_UPPER_PHRASES:
        return False
    if _is_catalog_orientation_line(raw):
        return False
    if ":" in raw and not _OFFICIAL_NAME_RE.match(raw):
        return False
    if any(word in n.split() for word in ("autonomia", "nominal", "alimentacao", "alimentação", "estacao", "estação", "peso", "operacional", "horas")):
        return False
    tokens = n.split()
    if len(tokens) > 5:
        return False
    has_digit = any(any(ch.isdigit() for ch in token) for token in tokens)
    all_capsish = bool(re.fullmatch(r"[A-Z0-9][A-Z0-9 -]{1,40}", raw))
    title_case_name = len(tokens) >= 2 and all(part[:1].isupper() for part in raw.split() if part[:1].isalpha())
    return has_digit or (all_capsish and len(tokens) > 1) or title_case_name


def _looks_like_model_name(value: str) -> bool:
    n = normalize_entity_text(value)
    return bool(n and any(ch.isdigit() for ch in n) and len(n) <= 40)


def _canonicalize_name(value: str) -> str:
    cleaned = _clean_title(value)
    if not cleaned or _is_catalog_orientation_canonical(cleaned):
        return ""
    if "/" in cleaned:
        product = re.split(r"\s[—–-]\s", cleaned, maxsplit=1)[0].strip()
        words = [w for w in product.split() if normalize_entity_text(w) not in _GENERIC_TITLE_WORDS]
        if words:
            return " ".join(words[:6]).strip()
    words = [w for w in cleaned.split() if normalize_entity_text(w) not in _GENERIC_TITLE_WORDS]
    if not words:
        return ""
    return " ".join(words[:4]).strip()


def _is_catalog_orientation_line(value: str) -> bool:
    raw = str(value or "").strip()
    return bool(_CATALOG_ORIENTATION_RE.search(raw))


def _is_catalog_orientation_canonical(value: str) -> bool:
    return _is_catalog_orientation_line(value)


def _parse_catalog_orientation_product(value: str) -> str:
    raw = str(value or "").strip().lstrip("- ").strip()
    for sep in ("→", "->"):
        if sep in raw:
            product = raw.split(sep, 1)[1].strip()
            product = re.split(r"\s[—–-]\s", product, maxsplit=1)[0].strip()
            return _clean_title(product)
    return ""


def _parse_official_name_line(value: str) -> str:
    match = _OFFICIAL_NAME_RE.match(str(value or "").strip())
    if match:
        return _clean_title(match.group(1))
    return ""


def _product_name_from_heading(title: str) -> str:
    match = _PRODUCT_HEADING_RE.match(str(title or "").strip())
    if match:
        return _clean_title(match.group(1))
    return ""


def _document_scope(*, file_name: str, relative_path: str, text: str, product_names: list[str]) -> str:
    blob = normalize_entity_text(f"{file_name} {relative_path} {str(text or '')[:800]}")
    if any(token in blob for token in (
        "visao geral", "visão geral", "produtos oficiais", "orientacao rapida",
        "orientação rápida", "linha xyron", "nomenclatura institucional",
    )):
        return "catalog_overview"
    if len(product_names) >= 4:
        return "catalog_overview"
    if product_names and len(product_names) <= 2:
        return "product_dedicated"
    return "general"


def _entity_group_key(canonical: str, raw_name: str) -> str:
    canonical_n = normalize_entity_text(canonical)
    return canonical_n


def _entity_specificity(entity: KnowledgeEntity) -> float:
    scores = list((entity.metadata or {}).get("doc_specificity", {}).values())
    return max(scores) if scores else 0.0


def _entity_text_blob(entity: KnowledgeEntity) -> str:
    return normalize_entity_text(" ".join([entity.canonical_name, *entity.aliases]))


def _score_application_family_entity(
    entity: KnowledgeEntity,
    *,
    active_application: str,
    active_topic: str,
    msg_n: str,
) -> tuple[float, str]:
    application = active_application or {
        "cleaning_robot": "cleaning_robotics",
        "security_robot": "security_robotics",
        "educational_robot": "educational_robotics",
    }.get(active_topic, "")
    if not application:
        return 0.0, "none"
    query_markers = _APPLICATION_QUERY_MARKERS.get(application, ())
    entity_hints = _APPLICATION_ENTITY_HINTS.get(application, ())
    if not any(marker in msg_n for marker in query_markers):
        return 0.0, "none"
    blob = _entity_text_blob(entity)
    if not any(hint in blob for hint in entity_hints):
        return 0.0, "none"
    specificity = _entity_specificity(entity)
    return 0.78 + specificity * 0.15, "application_family"


def _clean_title(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip(" -_|"))
    text = re.sub(r"\.(pdf|docx|md|txt)$", "", text, flags=re.IGNORECASE)
    return text[:120].strip()


def _dedupe_preserve(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        value = str(value or "").strip()
        key = normalize_entity_text(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _contains_phrase(text_n: str, phrase_n: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase_n)}(?!\w)", text_n) is not None


def _nearest_markdown_heading(text: str) -> str | None:
    heading = None
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+", stripped):
            heading = stripped.lstrip("#").strip()[:120]
    return heading


def _page_number_hint(text: str) -> int | None:
    match = re.search(r"(?:^|\n)\s*(?:pagina|página|page)\s+(\d{1,4})\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None
