from prospecting.application.enrichments import add_prospect_enrichment
from prospecting.models import ProspectEnrichment, SearchResult


def persist_search_result_observations(*, tenant, prospect, search_result: SearchResult):
    """Persist Maps observations as ProspectEnrichment without creating ProspectContact."""
    reference = (search_result.maps_url or search_result.name or "")[:240]
    if search_result.phone:
        add_prospect_enrichment(
            tenant=tenant,
            prospect=prospect,
            field=ProspectEnrichment.Field.PHONE,
            value=search_result.phone,
            source_type=ProspectEnrichment.SourceType.SEARCH_RESULT,
            source_search_result=search_result,
            source_reference=reference,
        )
    if search_result.address:
        add_prospect_enrichment(
            tenant=tenant,
            prospect=prospect,
            field=ProspectEnrichment.Field.ADDRESS,
            value=search_result.address,
            source_type=ProspectEnrichment.SourceType.SEARCH_RESULT,
            source_search_result=search_result,
            source_reference=reference,
        )
    if search_result.website:
        add_prospect_enrichment(
            tenant=tenant,
            prospect=prospect,
            field=ProspectEnrichment.Field.WEBSITE,
            value=search_result.website,
            source_type=ProspectEnrichment.SourceType.SEARCH_RESULT,
            source_search_result=search_result,
            source_reference=reference,
        )
