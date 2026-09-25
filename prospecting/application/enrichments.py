from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from prospecting.domain.enrichment import build_source_key, contains_forbidden_secret, normalize_enrichment_value, normalize_website
from prospecting.models import Prospect, ProspectEnrichment, SearchResult


def derive_domain_from_website(value):
    return normalize_enrichment_value(ProspectEnrichment.Field.DOMAIN, value)


@transaction.atomic
def add_prospect_enrichment(
    *,
    tenant,
    prospect,
    field,
    value,
    source_type=ProspectEnrichment.SourceType.MANUAL,
    source_search_result=None,
    source_url="",
    source_reference="",
    observed_at=None,
):
    locked_prospect = Prospect.objects.select_for_update().get(pk=prospect.pk)
    if locked_prospect.tenant_id != tenant.id:
        raise ValidationError("Prospect does not belong to the tenant.")

    if source_search_result is not None:
        source_search_result = SearchResult.objects.select_related("tenant").get(pk=source_search_result.pk)
        if source_search_result.tenant_id != tenant.id:
            raise ValidationError("Source SearchResult does not belong to the tenant.")

    field = str(field or "").upper()
    source_type = str(source_type or "").upper()
    raw_value = " ".join((value or "").split())
    if field not in ProspectEnrichment.Field.values:
        raise ValidationError("Unsupported enrichment field.")
    if source_type not in ProspectEnrichment.SourceType.values:
        raise ValidationError("Unsupported enrichment source type.")
    if not raw_value:
        raise ValidationError("Enrichment value is required.")
    if any(contains_forbidden_secret(item) for item in [raw_value, source_url, source_reference]):
        raise ValidationError("Enrichment cannot store credentials, tokens, cookies, or Authorization data.")

    normalized_value = normalize_enrichment_value(field, raw_value)
    normalized_source_url = normalize_website(source_url) if source_url else ""
    source_key = build_source_key(
        source_type=source_type,
        source_search_result=source_search_result,
        source_url=normalized_source_url,
        source_reference=source_reference,
    )
    enrichment, _ = ProspectEnrichment.objects.select_for_update().get_or_create(
        tenant=tenant,
        prospect=locked_prospect,
        field=field,
        normalized_value=normalized_value,
        source_key=source_key,
        defaults={
            "value": raw_value,
            "source_type": source_type,
            "source_search_result": source_search_result,
            "source_url": normalized_source_url,
            "source_reference": " ".join((source_reference or "").split()),
            "observed_at": observed_at or timezone.now(),
        },
    )
    return enrichment
