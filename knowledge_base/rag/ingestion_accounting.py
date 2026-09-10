from __future__ import annotations

from collections import Counter

from knowledge_base.models import TenantRagDriveFileManifest

INGESTED_STATUSES = {
    TenantRagDriveFileManifest.Status.EXPORTED,
    TenantRagDriveFileManifest.Status.UPDATED,
    TenantRagDriveFileManifest.Status.UNCHANGED,
    TenantRagDriveFileManifest.Status.DISCOVERED,
}

ACCOUNTED_NON_INGESTED = {
    TenantRagDriveFileManifest.Status.SKIPPED_TEMPORARY_ARTIFACT,
    TenantRagDriveFileManifest.Status.SKIPPED_UNSUPPORTED,
    TenantRagDriveFileManifest.Status.FAILED,
    TenantRagDriveFileManifest.Status.UNAVAILABLE,
    TenantRagDriveFileManifest.Status.REMOVED,
}


def is_office_temporary_artifact(filename: str) -> bool:
    """Detect Microsoft Office lock files (~$*.docx/xlsx/pptx)."""
    name = str(filename or "").strip()
    if not name.startswith("~$"):
        return False
    lowered = name.lower()
    return lowered.endswith((".docx", ".xlsx", ".pptx", ".xls", ".ppt", ".doc"))


def summarize_manifest_accounting(manifests) -> dict[str, int | bool]:
    """Summarize manifests into DISCOVERED = INGESTED + SKIPPED_TEMP + UNSUPPORTED + FAILED buckets."""
    statuses = Counter(getattr(item, "status", "") for item in manifests)
    discovered = sum(statuses.values())
    ingested = sum(
        statuses.get(status, 0)
        for status in (
            TenantRagDriveFileManifest.Status.EXPORTED,
            TenantRagDriveFileManifest.Status.UPDATED,
            TenantRagDriveFileManifest.Status.UNCHANGED,
        )
    )
    skipped_temporary = statuses.get(TenantRagDriveFileManifest.Status.SKIPPED_TEMPORARY_ARTIFACT, 0)
    unsupported = statuses.get(TenantRagDriveFileManifest.Status.SKIPPED_UNSUPPORTED, 0)
    failed = statuses.get(TenantRagDriveFileManifest.Status.FAILED, 0)
    unavailable = statuses.get(TenantRagDriveFileManifest.Status.UNAVAILABLE, 0)
    removed = statuses.get(TenantRagDriveFileManifest.Status.REMOVED, 0)
    discovered_only = statuses.get(TenantRagDriveFileManifest.Status.DISCOVERED, 0)

    equation_lhs = discovered
    equation_rhs = ingested + skipped_temporary + unsupported + failed + unavailable + removed + discovered_only
    return {
        "DISCOVERED_TOTAL": discovered,
        "INGESTED": ingested,
        "SKIPPED_TEMPORARY": skipped_temporary,
        "UNSUPPORTED": unsupported,
        "FAILED": failed,
        "UNAVAILABLE": unavailable,
        "REMOVED": removed,
        "DISCOVERED_PENDING": discovered_only,
        "EQUATION_BALANCED": equation_lhs == equation_rhs,
    }
