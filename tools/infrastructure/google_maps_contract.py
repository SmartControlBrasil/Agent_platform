from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from django.core.exceptions import ValidationError

from tools.models import ToolExecution, validate_no_plaintext_secrets

SCHEMA_VERSION = 1
MAX_QUERIES = 20
MAX_QUERY_CHARS = 200
MAX_REGION_CHARS = 120
MAX_LOCALE_CHARS = 16
MAX_RESULTS = 100
MAX_INPUT_BYTES = 16 * 1024
MAX_RESULT_BYTES = 256 * 1024
DEFAULT_LOCALE = "pt-BR"

STAT_KEYS = {
    "queries_requested",
    "queries_executed",
    "businesses_found",
    "businesses_returned",
    "duplicates_removed",
    "duration_ms",
}


def validate_google_maps_input(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("Google Maps input must be an object.")
    _validate_size(payload, MAX_INPUT_BYTES, "Google Maps input is too large.")
    validate_no_plaintext_secrets(payload)

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError("schema_version must be 1.")

    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValidationError("queries must contain at least one search query.")
    if len(queries) > MAX_QUERIES:
        raise ValidationError("queries cannot contain more than 20 items.")

    normalized_queries = [_normalize_query(query) for query in queries]
    target_region = _optional_text(payload.get("target_region"), "target_region", MAX_REGION_CHARS)
    locale = _optional_text(payload.get("locale", DEFAULT_LOCALE), "locale", MAX_LOCALE_CHARS) or DEFAULT_LOCALE
    max_results = _integer_between(payload.get("max_results"), "max_results", 1, MAX_RESULTS)

    return {
        "schema_version": SCHEMA_VERSION,
        "queries": normalized_queries,
        "target_region": target_region,
        "max_results": max_results,
        "locale": locale,
    }


def validate_google_maps_result(result: dict[str, Any], execution: ToolExecution | None = None) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValidationError("Google Maps result must be an object.")
    _validate_size(result, MAX_RESULT_BYTES, "Google Maps result is too large.")
    validate_no_plaintext_secrets(result)

    if result.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError("schema_version must be 1.")
    status = _required_text(result.get("status"), "status", 40)
    businesses = result.get("businesses")
    if not isinstance(businesses, list):
        raise ValidationError("businesses must be a list.")

    requested_max = _requested_max_results(execution)
    if len(businesses) > requested_max:
        raise ValidationError("businesses exceeds max_results.")

    normalized = [_normalize_business(item) for item in businesses]
    deduped, duplicates_removed = _dedupe_businesses(normalized)
    stats = _normalize_stats(result.get("stats") or {})
    stats["businesses_returned"] = len(deduped)
    stats["duplicates_removed"] = max(stats.get("duplicates_removed", 0), duplicates_removed)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "businesses": deduped,
        "stats": stats,
    }


def summarize_google_maps_execution(execution: ToolExecution) -> dict[str, Any]:
    input_payload = (execution.request_payload or {}).get("input") or {}
    result_payload = execution.result_payload or {}
    stats = result_payload.get("stats") or {}
    businesses = result_payload.get("businesses") or []
    return {
        "kind": "google_maps",
        "queries": input_payload.get("queries") or [],
        "result_count": len(businesses),
        "status": result_payload.get("status") or execution.status,
        "executor": execution.executor.name if execution.executor else "",
        "duration_ms": stats.get("duration_ms"),
        "preview": businesses[:10],
    }


def _normalize_query(value: Any) -> str:
    text = _required_text(value, "queries", MAX_QUERY_CHARS)
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, flags=re.IGNORECASE):
        raise ValidationError("queries must be search terms, not arbitrary URLs.")
    return text


def _normalize_business(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("Each business must be an object.")
    business = {
        "name": _required_text(value.get("name"), "business.name", 200),
        "category": _nullable_text(value.get("category"), "business.category", 120),
        "address": _nullable_text(value.get("address"), "business.address", 300),
        "phone": _nullable_text(value.get("phone"), "business.phone", 80),
        "website": _nullable_url(value.get("website"), "business.website"),
        "maps_url": _nullable_url(value.get("maps_url"), "business.maps_url"),
        "external_id": _nullable_text(value.get("external_id"), "business.external_id", 200),
        "source_query": _nullable_text(value.get("source_query"), "business.source_query", MAX_QUERY_CHARS),
    }
    if not business["maps_url"] and not business["external_id"]:
        raise ValidationError("Each business requires maps_url or external_id.")
    return business


def _dedupe_businesses(businesses: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen = set()
    deduped = []
    for business in businesses:
        key = _business_key(business)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(business)
    return deduped, len(businesses) - len(deduped)


def _business_key(business: dict[str, Any]) -> tuple[str, str]:
    if business.get("external_id"):
        return ("external_id", business["external_id"].casefold())
    if business.get("maps_url"):
        return ("maps_url", business["maps_url"].casefold())
    return ("name_address", f"{business['name']}|{business.get('address') or ''}".casefold())


def _normalize_stats(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValidationError("stats must be an object.")
    stats = {}
    for key in STAT_KEYS:
        raw = value.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValidationError(f"stats.{key} must be a non-negative integer.")
        stats[key] = raw
    return stats


def _requested_max_results(execution: ToolExecution | None) -> int:
    if execution is None:
        return MAX_RESULTS
    input_payload = (execution.request_payload or {}).get("input") or {}
    value = input_payload.get("max_results", MAX_RESULTS)
    return _integer_between(value, "max_results", 1, MAX_RESULTS)


def _required_text(value: Any, field_name: str, limit: int) -> str:
    text = _collapse_text(value)
    if not text:
        raise ValidationError(f"{field_name} is required.")
    if len(text) > limit:
        raise ValidationError(f"{field_name} is too long.")
    return text


def _optional_text(value: Any, field_name: str, limit: int) -> str:
    if value in (None, ""):
        return ""
    return _required_text(value, field_name, limit)


def _nullable_text(value: Any, field_name: str, limit: int) -> str | None:
    text = _optional_text(value, field_name, limit)
    return text or None


def _nullable_url(value: Any, field_name: str) -> str | None:
    text = _nullable_text(value, field_name, 500)
    if text is None:
        return None
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValidationError(f"{field_name} must be an absolute HTTP URL.")
    path = parts.path or ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _integer_between(value: Any, field_name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{field_name} must be an integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field_name} must be an integer.") from exc
    if number < minimum or number > maximum:
        raise ValidationError(f"{field_name} must be between {minimum} and {maximum}.")
    return number


def _collapse_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("Text fields must be strings.")
    return " ".join(value.strip().split())


def _validate_size(value: dict[str, Any], max_bytes: int, message: str) -> None:
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > max_bytes:
        raise ValidationError(message)
