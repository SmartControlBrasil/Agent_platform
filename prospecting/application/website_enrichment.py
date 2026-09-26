from dataclasses import dataclass, field

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from prospecting.application.enrichments import add_prospect_enrichment, derive_domain_from_website
from prospecting.domain.enrichment import build_source_key, normalize_enrichment_value, normalize_website
from prospecting.domain.website_enrichment import canonical_url, contact_link_candidates, extract_contacts
from prospecting.models import Prospect, ProspectEnrichment


@dataclass
class ProspectWebsiteEnrichmentResult:
    prospect_id: str
    pages_fetched: int = 0
    emails_found: int = 0
    phones_found: int = 0
    addresses_found: int = 0
    enrichments_created: int = 0
    enrichments_existing: int = 0
    warnings: list[str] = field(default_factory=list)


def _official_website_for(prospect):
    if prospect.website:
        return normalize_website(prospect.website)
    websites = list(
        prospect.enrichments.filter(field=ProspectEnrichment.Field.WEBSITE).order_by("created_at").values_list("value", flat=True)[:2]
    )
    if len(websites) == 1:
        return normalize_website(websites[0])
    return ""


def _add(result, *, tenant, prospect, field, value, source_url):
    normalized_value = normalize_enrichment_value(field, value)
    source_key = build_source_key(source_type=ProspectEnrichment.SourceType.WEBSITE, source_url=source_url)
    existed = ProspectEnrichment.objects.filter(
        tenant=tenant,
        prospect=prospect,
        field=field,
        normalized_value=normalized_value,
        source_key=source_key,
    ).exists()
    enrichment = add_prospect_enrichment(
        tenant=tenant,
        prospect=prospect,
        field=field,
        value=value,
        source_type=ProspectEnrichment.SourceType.WEBSITE,
        source_url=source_url,
    )
    if not existed:
        result.enrichments_created += 1
    else:
        result.enrichments_existing += 1
    return enrichment


def _persist_observations(*, tenant, prospect, page_url, emails, phones, addresses, result):
    for email in sorted(emails):
        result.emails_found += 1
        _add(result, tenant=tenant, prospect=prospect, field=ProspectEnrichment.Field.EMAIL, value=email, source_url=page_url)
    for phone in sorted(phones):
        result.phones_found += 1
        _add(result, tenant=tenant, prospect=prospect, field=ProspectEnrichment.Field.PHONE, value=phone, source_url=page_url)
    for address in sorted(addresses):
        result.addresses_found += 1
        _add(result, tenant=tenant, prospect=prospect, field=ProspectEnrichment.Field.ADDRESS, value=address, source_url=page_url)


@transaction.atomic
def enrich_prospect_from_website(*, tenant, prospect, fetcher=None):
    locked = Prospect.objects.select_for_update().get(pk=prospect.pk)
    if locked.tenant_id != tenant.id:
        raise ValidationError("Prospect does not belong to the tenant.")
    website = _official_website_for(locked)
    if not website:
        raise ValidationError("Prospect has no website available for enrichment.")

    if fetcher is None:
        from prospecting.infrastructure.website_fetcher import WebsiteHttpFetcher

        fetcher = WebsiteHttpFetcher()

    result = ProspectWebsiteEnrichmentResult(prospect_id=str(locked.id))
    max_pages = settings.WEBSITE_ENRICHMENT_MAX_PAGES
    home_url = canonical_url(website)

    # DOMAIN is deterministic from the official website and uses the website itself as provenance.
    _add(result, tenant=tenant, prospect=locked, field=ProspectEnrichment.Field.DOMAIN, value=derive_domain_from_website(home_url), source_url=home_url)

    try:
        home = fetcher.fetch(home_url)
    except ValidationError:
        raise
    result.pages_fetched += 1
    emails, phones, addresses, links = extract_contacts(home.body, page_url=home.url)
    _persist_observations(
        tenant=tenant,
        prospect=locked,
        page_url=home.url,
        emails=emails,
        phones=phones,
        addresses=addresses,
        result=result,
    )

    candidates = contact_link_candidates(links, base_url=home_url, max_links=max(0, max_pages - 1))
    fetched = {canonical_url(home.url)}
    for candidate in candidates:
        if result.pages_fetched >= max_pages:
            break
        candidate_url = canonical_url(candidate.url)
        if candidate_url in fetched:
            continue
        fetched.add(candidate_url)
        try:
            page = fetcher.fetch(candidate_url)
        except ValidationError as exc:
            result.warnings.extend(getattr(exc, "messages", [str(exc)]))
            continue
        result.pages_fetched += 1
        emails, phones, addresses, _links = extract_contacts(page.body, page_url=page.url)
        _persist_observations(
            tenant=tenant,
            prospect=locked,
            page_url=page.url,
            emails=emails,
            phones=phones,
            addresses=addresses,
            result=result,
        )
    return result
