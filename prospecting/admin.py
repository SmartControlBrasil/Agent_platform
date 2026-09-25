from django.contrib import admin

from .models import Prospect, ProspectEnrichment, ProspectSource, SearchResult, SearchRun


@admin.register(SearchRun)
class SearchRunAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "project", "agent_installation", "status", "target_region", "max_results", "created_at")
    list_filter = ("status", "tenant")
    search_fields = ("id", "project__name", "agent_installation__name", "target_region", "idempotency_key")
    readonly_fields = ("id", "idempotency_key", "created_at", "updated_at")


@admin.register(SearchResult)
class SearchResultAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "search_run", "category", "created_at")
    list_filter = ("tenant",)
    search_fields = ("name", "address", "external_id", "dedupe_key")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Prospect)
class ProspectAdmin(admin.ModelAdmin):
    list_display = ("display_name", "tenant", "external_id", "created_at")
    list_filter = ("tenant",)
    search_fields = ("display_name", "address", "phone", "website", "maps_url", "external_id", "identity_key")
    readonly_fields = ("id", "identity_key", "created_at", "updated_at")


@admin.register(ProspectSource)
class ProspectSourceAdmin(admin.ModelAdmin):
    list_display = ("prospect", "tenant", "search_result", "search_run", "created_at")
    list_filter = ("tenant",)
    search_fields = ("prospect__display_name", "search_result__name", "search_run__id")
    readonly_fields = ("id", "created_at")


@admin.register(ProspectEnrichment)
class ProspectEnrichmentAdmin(admin.ModelAdmin):
    list_display = ("prospect", "tenant", "field", "value", "source_type", "observed_at")
    list_filter = ("tenant", "field", "source_type")
    search_fields = ("prospect__display_name", "value", "normalized_value", "source_reference", "source_url")
    readonly_fields = ("id", "normalized_value", "source_key", "created_at", "updated_at")
