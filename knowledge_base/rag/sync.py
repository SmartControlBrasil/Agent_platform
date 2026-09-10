from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from knowledge_base.models import (
    TenantRagConfiguration,
    TenantRagDocumentChunk,
    TenantRagDriveFileManifest,
    TenantRagDriveTextStaging,
)
from knowledge_base.rag.chunking import RagChunkingError, build_deterministic_chunks, load_chunk_config
from knowledge_base.rag.entity_catalog import build_chunk_metadata, extract_document_metadata
from knowledge_base.rag.google_drive_inventory import (
    GOOGLE_DRIVE_SHORTCUT_MIME,
    GoogleDriveApiError,
    compute_text_sha256,
    decode_google_text_payload,
    normalize_text_for_rag,
    sanitize_external_error_message,
)
from knowledge_base.rag.ingestion_accounting import is_office_temporary_artifact
from tenants.models import Tenant

logger = logging.getLogger(__name__)

SUPPORTED_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
    "text/html",
}


@dataclass(frozen=True)
class SyncCounters:
    discovered: int = 0
    exported: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "discovered": self.discovered,
            "exported": self.exported,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "skipped": self.skipped,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class SyncOutcome:
    mode: str
    status: str
    counters: SyncCounters
    file_count: int
    folder_count: int
    blocked_shortcuts: int


class TenantRagSyncError(Exception):
    pass


def validate_rag_export_max_bytes() -> int:
    value = int(getattr(settings, "LIVIA_RAG_EXPORT_MAX_BYTES", 1000000) or 0)
    if value <= 0:
        raise TenantRagSyncError("LIVIA_RAG_EXPORT_MAX_BYTES must be a positive integer.")
    return value


def acquire_tenant_sync_lock(*, tenant: Tenant, mode: str) -> TenantRagConfiguration:
    timeout_seconds = int(getattr(settings, "LIVIA_RAG_SYNC_RUNNING_TIMEOUT_SECONDS", 1800) or 1800)
    timeout_seconds = max(timeout_seconds, 60)
    now = timezone.now()
    stale_cutoff = now - timedelta(seconds=timeout_seconds)

    with transaction.atomic():
        configuration = (
            TenantRagConfiguration.objects.select_for_update()
            .select_related("tenant")
            .filter(tenant=tenant)
            .first()
        )
        if configuration is None:
            raise TenantRagSyncError("Tenant RAG configuration not found. Run configure_tenant_rag first.")
        if mode != "build_chunks" and not configuration.sync_enabled:
            raise TenantRagSyncError("Tenant RAG sync is disabled. Re-run configure_tenant_rag with --enable-sync.")
        if mode != "build_chunks" and (not configuration.uses_google_drive or not configuration.approved_folder_id):
            raise TenantRagSyncError("Google Drive sync requires source_mode=google_drive and approved_folder_id.")
        if (
            configuration.last_inventory_status == TenantRagConfiguration.InventoryStatus.RUNNING
            and configuration.last_inventory_started_at
            and configuration.last_inventory_started_at > stale_cutoff
        ):
            raise TenantRagSyncError("Another sync for this tenant is already running.")

        configuration.last_inventory_status = TenantRagConfiguration.InventoryStatus.RUNNING
        configuration.last_inventory_started_at = now
        configuration.last_inventory_mode = mode
        configuration.last_inventory_error = ""
        configuration.save(
            update_fields=[
                "last_inventory_status",
                "last_inventory_started_at",
                "last_inventory_mode",
                "last_inventory_error",
                "updated_at",
            ]
        )
        return configuration


def run_sync_for_inventory(
    *,
    configuration: TenantRagConfiguration,
    mode: str,
    inventory_summary,
    drive_service,
) -> SyncOutcome:
    max_export_bytes = validate_rag_export_max_bytes()
    now = timezone.now()
    seen_ids: set[str] = set()
    counters = {
        "discovered": 0,
        "exported": 0,
        "updated": 0,
        "unchanged": 0,
        "removed": 0,
        "skipped": 0,
        "failed": 0,
    }
    has_partial_failures = False

    for file_record in inventory_summary.files:
        seen_ids.add(file_record.file_id)
        item_outcome = _process_inventory_item(
            configuration=configuration,
            mode=mode,
            file_record=file_record,
            drive_service=drive_service,
            max_export_bytes=max_export_bytes,
            seen_at=now,
        )
        for key in counters:
            counters[key] += item_outcome.get(key, 0)
        has_partial_failures = has_partial_failures or bool(item_outcome.get("failed", 0))

    # Removed items are only marked after a complete scan with no individual failures.
    if not has_partial_failures:
        counters["removed"] += mark_manifests_as_removed_if_missing(
            configuration=configuration,
            seen_drive_file_ids=seen_ids,
            seen_at=now,
        )

    final_status = (
        TenantRagConfiguration.InventoryStatus.PARTIAL
        if has_partial_failures
        else TenantRagConfiguration.InventoryStatus.SUCCESS
    )
    _finalize_configuration_state(
        configuration=configuration,
        status=final_status,
        error_message="Some files failed to export." if has_partial_failures else "",
        file_count=len(inventory_summary.files),
    )
    outcome = SyncOutcome(
        mode=mode,
        status=final_status,
        counters=SyncCounters(**counters),
        file_count=len(inventory_summary.files),
        folder_count=inventory_summary.traversed_folders,
        blocked_shortcuts=inventory_summary.blocked_shortcuts,
    )
    logger.info(
        "tenant_rag_sync_finished tenant_slug=%s mode=%s status=%s discovered=%s exported=%s updated=%s unchanged=%s removed=%s skipped=%s failed=%s",
        configuration.tenant.slug,
        mode,
        final_status,
        counters["discovered"],
        counters["exported"],
        counters["updated"],
        counters["unchanged"],
        counters["removed"],
        counters["skipped"],
        counters["failed"],
    )
    return outcome


def run_chunk_build_for_tenant(
    *,
    configuration: TenantRagConfiguration,
) -> SyncOutcome:
    config = load_chunk_config()
    tenant = configuration.tenant
    now = timezone.now()
    counters = {
        "discovered": 0,
        "exported": 0,
        "updated": 0,
        "unchanged": 0,
        "removed": 0,
        "skipped": 0,
        "failed": 0,
    }
    # semantic aliases for chunk summary
    # discovered -> created docs
    # exported -> rebuilt docs
    # updated -> chunks_created
    # unchanged -> unchanged docs
    # removed -> deactivated docs
    # skipped -> skipped docs
    # failed -> failed docs
    partial = False

    staging_queryset = (
        TenantRagDriveTextStaging.objects.select_related("tenant", "manifest", "manifest__configuration")
        .filter(
            tenant=tenant,
            manifest__tenant=tenant,
            manifest__configuration=configuration,
            manifest__is_active=True,
        )
        .order_by("manifest__drive_file_id")
    )

    for staging in staging_queryset:
        result = _process_staging_for_chunks(staging=staging, chunk_config_signature=config.signature, now=now, chunk_config=config)
        for key in counters:
            counters[key] += int(result.get(key, 0))
        partial = partial or bool(result.get("failed", 0))

    # deactivate chunks for manifests that are no longer active
    deactivated = _deactivate_chunks_for_inactive_manifests(configuration=configuration)
    counters["removed"] += deactivated

    final_status = TenantRagConfiguration.InventoryStatus.PARTIAL if partial else TenantRagConfiguration.InventoryStatus.SUCCESS
    _finalize_configuration_state(
        configuration=configuration,
        status=final_status,
        error_message="Some documents failed during chunk build." if partial else "",
        file_count=staging_queryset.count(),
    )
    logger.info(
        "tenant_rag_chunk_build_finished tenant_slug=%s status=%s created=%s rebuilt=%s unchanged=%s deactivated=%s chunks_created=%s skipped=%s failed=%s",
        tenant.slug,
        final_status,
        counters["discovered"],
        counters["exported"],
        counters["unchanged"],
        counters["removed"],
        counters["updated"],
        counters["skipped"],
        counters["failed"],
    )
    return SyncOutcome(
        mode="build_chunks",
        status=final_status,
        counters=SyncCounters(**counters),
        file_count=staging_queryset.count(),
        folder_count=0,
        blocked_shortcuts=0,
    )


def mark_configuration_failed(configuration: TenantRagConfiguration, *, error_message: str) -> None:
    _finalize_configuration_state(
        configuration=configuration,
        status=TenantRagConfiguration.InventoryStatus.FAILED,
        error_message=error_message,
        file_count=0,
    )


def _process_inventory_item(
    *,
    configuration: TenantRagConfiguration,
    mode: str,
    file_record,
    drive_service,
    max_export_bytes: int,
    seen_at,
) -> dict[str, int]:
    tenant = configuration.tenant
    manifest, created = TenantRagDriveFileManifest.objects.get_or_create(
        tenant=tenant,
        drive_file_id=file_record.file_id,
        defaults={
            "configuration": configuration,
            "name": file_record.name,
            "mime_type": file_record.mime_type,
            "relative_path": file_record.relative_path,
            "drive_modified_time": _parse_drive_datetime(file_record.modified_time),
            "drive_size_bytes": file_record.size_bytes,
            "status": TenantRagDriveFileManifest.Status.DISCOVERED,
            "is_active": True,
            "last_seen_at": seen_at,
        },
    )
    if created:
        counters = {"discovered": 1, "exported": 0, "updated": 0, "unchanged": 0, "removed": 0, "skipped": 0, "failed": 0}
        previous_modified = None
        previous_sha = ""
    else:
        counters = {"discovered": 0, "exported": 0, "updated": 0, "unchanged": 0, "removed": 0, "skipped": 0, "failed": 0}
        previous_modified = manifest.drive_modified_time
        previous_sha = manifest.normalized_text_sha256

    if not created:
        manifest.configuration = configuration
        manifest.name = file_record.name
        manifest.mime_type = file_record.mime_type
        manifest.relative_path = file_record.relative_path
        manifest.drive_modified_time = _parse_drive_datetime(file_record.modified_time)
        manifest.drive_size_bytes = file_record.size_bytes
        manifest.last_seen_at = seen_at
        manifest.is_active = True
        manifest.removed_at = None
        manifest.last_error = ""
        manifest.save(
            update_fields=[
                "configuration",
                "name",
                "mime_type",
                "relative_path",
                "drive_modified_time",
                "drive_size_bytes",
                "last_seen_at",
                "is_active",
                "removed_at",
                "last_error",
                "updated_at",
            ]
        )
    if file_record.mime_type == GOOGLE_DRIVE_SHORTCUT_MIME:
        manifest.status = TenantRagDriveFileManifest.Status.UNAVAILABLE
        manifest.last_error = "Shortcuts are blocked for security reasons."
        manifest.save(update_fields=["status", "last_error", "updated_at"])
        counters["skipped"] += 1
        return counters

    if is_office_temporary_artifact(file_record.name):
        manifest.status = TenantRagDriveFileManifest.Status.SKIPPED_TEMPORARY_ARTIFACT
        manifest.last_error = "Microsoft Office temporary lock file."
        manifest.save(update_fields=["status", "last_error", "updated_at"])
        counters["skipped"] += 1
        return counters

    if file_record.mime_type not in SUPPORTED_EXPORT_MIME_TYPES:
        manifest.status = TenantRagDriveFileManifest.Status.SKIPPED_UNSUPPORTED
        manifest.last_error = ""
        manifest.save(update_fields=["status", "last_error", "updated_at"])
        counters["skipped"] += 1
        return counters

    if mode == "inventory_only":
        if created:
            manifest.status = TenantRagDriveFileManifest.Status.DISCOVERED
        else:
            manifest.status = TenantRagDriveFileManifest.Status.UNCHANGED
            counters["unchanged"] += 1
        manifest.last_error = ""
        manifest.save(update_fields=["status", "last_error", "updated_at"])
        return counters

    should_export = created or _document_appears_changed(
        previous_modified=previous_modified,
        previous_sha=previous_sha,
        file_record=file_record,
    )
    if not should_export:
        manifest.status = TenantRagDriveFileManifest.Status.UNCHANGED
        manifest.last_error = ""
        manifest.save(update_fields=["status", "last_error", "updated_at"])
        counters["unchanged"] += 1
        return counters

    existing_sha = previous_sha
    try:
        exported_payload = drive_service.export_file_text(file_record.file_id, file_record.mime_type)
        if len(exported_payload) > max_export_bytes:
            raise GoogleDriveApiError("Exported document exceeded maximum allowed bytes.")
        decoded = decode_google_text_payload(exported_payload)
        normalized = normalize_text_for_rag(decoded)
        text_hash = compute_text_sha256(normalized)
        extraction_meta = getattr(drive_service, "last_extraction_metadata", None) or {}
        _persist_exported_document(
            manifest=manifest,
            normalized_text=normalized,
            text_hash=text_hash,
            exported_at=seen_at,
            extraction_metadata=extraction_meta,
        )
    except Exception as exc:
        safe_error = sanitize_external_error_message(str(exc))
        manifest.status = TenantRagDriveFileManifest.Status.FAILED
        manifest.last_error = safe_error
        manifest.save(update_fields=["status", "last_error", "updated_at"])
        counters["failed"] += 1
        return counters

    if not existing_sha:
        counters["exported"] += 1
    elif existing_sha != manifest.normalized_text_sha256:
        counters["updated"] += 1
    else:
        counters["unchanged"] += 1
    return counters


def _persist_exported_document(
    *,
    manifest: TenantRagDriveFileManifest,
    normalized_text: str,
    text_hash: str,
    exported_at,
    extraction_metadata: dict | None = None,
):
    document_metadata = extract_document_metadata(
        file_name=manifest.name,
        mime_type=manifest.mime_type,
        relative_path=manifest.relative_path,
        text=normalized_text,
        source_modified_time=manifest.drive_modified_time,
    )
    document_metadata["source_document_id"] = manifest.drive_file_id
    if extraction_metadata:
        document_metadata["extraction"] = extraction_metadata
    with transaction.atomic():
        locked = TenantRagDriveFileManifest.objects.select_for_update().get(pk=manifest.pk)
        staging, _ = TenantRagDriveTextStaging.objects.select_for_update().get_or_create(
            manifest=locked,
            defaults={
                "tenant": locked.tenant,
                "normalized_text": "",
                "normalized_text_sha256": text_hash,
                "normalized_text_char_count": 0,
                "normalized_text_byte_count": 0,
                "exported_at": exported_at,
            },
        )
        staging.normalized_text = normalized_text
        staging.normalized_text_sha256 = text_hash
        staging.normalized_text_char_count = len(normalized_text)
        staging.normalized_text_byte_count = len(normalized_text.encode("utf-8"))
        staging.exported_at = exported_at
        staging.save(
            update_fields=[
                "normalized_text",
                "normalized_text_sha256",
                "normalized_text_char_count",
                "normalized_text_byte_count",
                "exported_at",
                "updated_at",
            ]
        )

        locked.document_metadata = document_metadata
        locked.normalized_text_sha256 = text_hash
        locked.status = (
            TenantRagDriveFileManifest.Status.EXPORTED
            if not locked.last_exported_at
            else TenantRagDriveFileManifest.Status.UPDATED
        )
        locked.last_exported_at = exported_at
        locked.last_error = ""
        locked.save(
            update_fields=[
                "document_metadata",
                "normalized_text_sha256",
                "status",
                "last_exported_at",
                "last_error",
                "updated_at",
            ]
        )
        manifest.document_metadata = locked.document_metadata
        manifest.normalized_text_sha256 = locked.normalized_text_sha256
        manifest.status = locked.status
        manifest.last_exported_at = locked.last_exported_at


def _process_staging_for_chunks(*, staging: TenantRagDriveTextStaging, chunk_config_signature: str, now, chunk_config) -> dict[str, int]:
    manifest = staging.manifest
    counters = {"discovered": 0, "exported": 0, "updated": 0, "unchanged": 0, "removed": 0, "skipped": 0, "failed": 0}

    if not staging.normalized_text.strip():
        counters["skipped"] += 1
        return counters

    active_chunks = list(
        TenantRagDocumentChunk.objects.filter(
            tenant=staging.tenant,
            manifest=manifest,
            is_active=True,
        ).order_by("ordinal")
    )
    has_chunks = bool(active_chunks)
    same_version = (
        has_chunks
        and all(chunk.source_text_sha256 == staging.normalized_text_sha256 for chunk in active_chunks)
        and all(chunk.chunk_config_signature == chunk_config_signature for chunk in active_chunks)
    )
    if same_version:
        counters["unchanged"] += 1
        return counters

    try:
        chunk_records = build_deterministic_chunks(staging.normalized_text, chunk_config)
    except RagChunkingError as exc:
        _mark_chunk_generation_failed(manifest=manifest, safe_error=sanitize_external_error_message(str(exc)))
        counters["failed"] += 1
        return counters

    if not chunk_records:
        counters["skipped"] += 1
        return counters

    try:
        with transaction.atomic():
            current_active = list(
                TenantRagDocumentChunk.objects.select_for_update()
                .filter(tenant=staging.tenant, manifest=manifest, is_active=True)
                .order_by("ordinal")
            )
            reusable = list(
                TenantRagDocumentChunk.objects.select_for_update()
                .filter(
                    tenant=staging.tenant,
                    manifest=manifest,
                    is_active=False,
                    source_text_sha256=staging.normalized_text_sha256,
                    chunk_config_signature=chunk_config_signature,
                )
                .order_by("ordinal")
            )
            if _can_reactivate_chunk_set(reusable, chunk_records):
                if current_active:
                    TenantRagDocumentChunk.objects.filter(pk__in=[chunk.pk for chunk in current_active]).update(
                        is_active=False,
                        status=TenantRagDocumentChunk.Status.REPLACED,
                        updated_at=now,
                    )
                TenantRagDocumentChunk.objects.filter(pk__in=[chunk.pk for chunk in reusable]).update(
                    is_active=True,
                    status=TenantRagDocumentChunk.Status.ACTIVE,
                    staging=staging,
                    updated_at=now,
                )
                counters["exported"] += 1
                return counters

            created_docs = 0 if current_active else 1
            rebuilt_docs = 1 if current_active else 0
            if current_active:
                TenantRagDocumentChunk.objects.filter(pk__in=[chunk.pk for chunk in current_active]).update(
                    is_active=False,
                    status=TenantRagDocumentChunk.Status.REPLACED,
                    updated_at=now,
                )
            new_chunks = []
            for record in chunk_records:
                new_chunks.append(
                    TenantRagDocumentChunk(
                        tenant=staging.tenant,
                        manifest=manifest,
                        staging=staging,
                        ordinal=record.ordinal,
                        chunk_text=record.text,
                        chunk_sha256=record.chunk_sha256,
                        source_text_sha256=staging.normalized_text_sha256,
                        chunk_config_signature=chunk_config_signature,
                        char_count=record.char_count,
                        byte_count=record.byte_count,
                        start_char=record.start_char,
                        end_char=record.end_char,
                        chunk_metadata=build_chunk_metadata(
                            document_metadata=manifest.document_metadata,
                            text=record.text,
                            start_char=record.start_char,
                        ),
                        status=TenantRagDocumentChunk.Status.ACTIVE,
                        is_active=True,
                    )
                )
            TenantRagDocumentChunk.objects.bulk_create(new_chunks)
            counters["discovered"] += created_docs
            counters["exported"] += rebuilt_docs
            counters["updated"] += len(new_chunks)
    except Exception as exc:  # pragma: no cover - defensive path
        _mark_chunk_generation_failed(manifest=manifest, safe_error=sanitize_external_error_message(str(exc)))
        counters["failed"] += 1
        return counters
    manifest.last_error = ""
    manifest.save(update_fields=["last_error", "updated_at"])
    return counters


def _deactivate_chunks_for_inactive_manifests(*, configuration: TenantRagConfiguration) -> int:
    queryset = TenantRagDocumentChunk.objects.filter(
        tenant=configuration.tenant,
        manifest__configuration=configuration,
        is_active=True,
        manifest__is_active=False,
    )
    count = queryset.count()
    if count:
        queryset.update(
            is_active=False,
            status=TenantRagDocumentChunk.Status.REPLACED,
        )
    return count


def _mark_chunk_generation_failed(*, manifest: TenantRagDriveFileManifest, safe_error: str):
    manifest.last_error = safe_error
    manifest.save(update_fields=["last_error", "updated_at"])


def _can_reactivate_chunk_set(chunks: list[TenantRagDocumentChunk], chunk_records) -> bool:
    if not chunks or len(chunks) != len(chunk_records):
        return False
    for existing, new in zip(chunks, chunk_records):
        if existing.ordinal != new.ordinal:
            return False
        if existing.chunk_sha256 != new.chunk_sha256:
            return False
        if existing.start_char != new.start_char or existing.end_char != new.end_char:
            return False
    return True


def mark_manifests_as_removed_if_missing(*, configuration: TenantRagConfiguration, seen_drive_file_ids: set[str], seen_at) -> int:
    from knowledge_base.services.manual_rag import MANUAL_SOURCE_PREFIX

    queryset = (
        TenantRagDriveFileManifest.objects.filter(
            tenant=configuration.tenant,
            configuration=configuration,
            is_active=True,
        )
        .exclude(drive_file_id__in=seen_drive_file_ids)
        .exclude(drive_file_id__startswith=f"{MANUAL_SOURCE_PREFIX}-")
    )
    removed_count = queryset.count()
    if removed_count:
        queryset.update(
            is_active=False,
            status=TenantRagDriveFileManifest.Status.REMOVED,
            removed_at=seen_at,
            last_error="",
        )
    return removed_count


def _document_appears_changed(*, previous_modified, previous_sha: str, file_record) -> bool:
    current_modified = _parse_drive_datetime(file_record.modified_time)
    return (
        not previous_sha
        or previous_modified != current_modified
    )


def _parse_drive_datetime(value: str):
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed


def _finalize_configuration_state(*, configuration: TenantRagConfiguration, status: str, error_message: str, file_count: int):
    configuration.last_inventory_status = status
    configuration.last_inventory_at = timezone.now()
    configuration.last_inventory_file_count = max(int(file_count or 0), 0)
    configuration.last_inventory_error = sanitize_external_error_message(error_message)
    configuration.save(
        update_fields=[
            "last_inventory_status",
            "last_inventory_at",
            "last_inventory_file_count",
            "last_inventory_error",
            "updated_at",
        ]
    )
