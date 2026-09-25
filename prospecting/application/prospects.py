from django.core.exceptions import ValidationError
from django.db import transaction

from prospecting.domain.prospect_identity import build_prospect_identity_key
from prospecting.models import Prospect, ProspectSource, SearchResult


def _prospect_defaults_from_search_result(search_result, identity_key):
    return {
        "display_name": search_result.name,
        "address": search_result.address,
        "phone": search_result.phone,
        "website": search_result.website,
        "maps_url": search_result.maps_url,
        "external_id": search_result.external_id,
        "identity_key": identity_key,
    }


def _fill_missing_prospect_fields(prospect, search_result):
    changed = False
    for prospect_field, result_field in [
        ("address", "address"),
        ("phone", "phone"),
        ("website", "website"),
        ("maps_url", "maps_url"),
        ("external_id", "external_id"),
    ]:
        if not getattr(prospect, prospect_field) and getattr(search_result, result_field):
            setattr(prospect, prospect_field, getattr(search_result, result_field))
            changed = True
    if changed:
        prospect.save(update_fields=["address", "phone", "website", "maps_url", "external_id", "updated_at"])
    return changed


@transaction.atomic
def promote_search_result_to_prospect(*, tenant, search_result):
    locked = SearchResult.objects.select_for_update().select_related("tenant", "search_run").get(pk=search_result.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("SearchResult does not belong to the tenant.")

    existing_source = (
        ProspectSource.objects.select_for_update()
        .select_related("prospect")
        .filter(tenant=tenant, search_result=locked)
        .first()
    )
    if existing_source:
        return existing_source.prospect

    identity_key = build_prospect_identity_key(locked)
    prospect, _ = Prospect.objects.select_for_update().get_or_create(
        tenant=tenant,
        identity_key=identity_key,
        defaults=_prospect_defaults_from_search_result(locked, identity_key),
    )
    _fill_missing_prospect_fields(prospect, locked)
    source, created = ProspectSource.objects.get_or_create(
        tenant=tenant,
        search_result=locked,
        defaults={"prospect": prospect, "search_run": locked.search_run},
    )
    if not created and source.prospect_id != prospect.id:
        return source.prospect
    return prospect
