from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from prospecting.application.prospects import promote_search_result_to_prospect
from prospecting.models import ProspectSource, SearchResult


class ProspectingReviewError(ValidationError):
    pass


@dataclass(frozen=True)
class BulkPromoteSummary:
    promoted: int = 0
    already_promoted: int = 0
    failed: int = 0


@dataclass(frozen=True)
class BulkIgnoreSummary:
    ignored: int = 0
    already_ignored: int = 0
    promoted_conflict: int = 0
    failed: int = 0


def mark_search_result_ignored(*, tenant, search_result) -> SearchResult:
    with transaction.atomic():
        locked = SearchResult.objects.select_for_update().select_related("tenant").get(pk=search_result.pk)
        if locked.tenant_id != tenant.id:
            raise ProspectingReviewError("SearchResult does not belong to the tenant.")
        if ProspectSource.objects.filter(tenant=tenant, search_result=locked).exists():
            raise ProspectingReviewError("SearchResult já promovido para Prospect não pode ser ignorado.")
        if locked.review_status != SearchResult.ReviewStatus.IGNORED:
            locked.review_status = SearchResult.ReviewStatus.IGNORED
            locked.save(update_fields=["review_status", "updated_at"])
        return locked


def restore_search_result_to_unreviewed(*, tenant, search_result) -> SearchResult:
    with transaction.atomic():
        locked = SearchResult.objects.select_for_update().select_related("tenant").get(pk=search_result.pk)
        if locked.tenant_id != tenant.id:
            raise ProspectingReviewError("SearchResult does not belong to the tenant.")
        if locked.review_status != SearchResult.ReviewStatus.UNREVIEWED:
            locked.review_status = SearchResult.ReviewStatus.UNREVIEWED
            locked.save(update_fields=["review_status", "updated_at"])
        return locked


def bulk_promote_search_results(*, tenant, search_results) -> BulkPromoteSummary:
    promoted = 0
    already_promoted = 0
    failed = 0
    for result in search_results:
        try:
            source_exists = ProspectSource.objects.filter(tenant=tenant, search_result=result).exists()
            if result.review_status == SearchResult.ReviewStatus.IGNORED:
                restore_search_result_to_unreviewed(tenant=tenant, search_result=result)
            promote_search_result_to_prospect(tenant=tenant, search_result=result)
            if source_exists:
                already_promoted += 1
            else:
                promoted += 1
        except ValidationError:
            failed += 1
    return BulkPromoteSummary(promoted=promoted, already_promoted=already_promoted, failed=failed)


def bulk_ignore_search_results(*, tenant, search_results) -> BulkIgnoreSummary:
    ignored = 0
    already_ignored = 0
    promoted_conflict = 0
    failed = 0
    for result in search_results:
        try:
            if ProspectSource.objects.filter(tenant=tenant, search_result=result).exists():
                promoted_conflict += 1
                continue
            if result.review_status == SearchResult.ReviewStatus.IGNORED:
                already_ignored += 1
                continue
            mark_search_result_ignored(tenant=tenant, search_result=result)
            ignored += 1
        except ValidationError:
            failed += 1
    return BulkIgnoreSummary(
        ignored=ignored,
        already_ignored=already_ignored,
        promoted_conflict=promoted_conflict,
        failed=failed,
    )
