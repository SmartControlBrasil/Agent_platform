from django.contrib import admin

from .models import (
    Prospect,
    ProspectActivity,
    ProspectContact,
    ProspectContactOutcome,
    ProspectEnrichment,
    ProspectFollowUp,
    ProspectOutreachDraft,
    ProspectOutreachSend,
    ProspectSource,
    SearchResult,
    SearchRun,
    SearchRunExecutionAttempt,
)


@admin.register(SearchRun)
class SearchRunAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "project", "agent_installation", "status", "target_region", "max_results", "created_at")
    list_filter = ("status", "tenant")
    search_fields = ("id", "project__name", "agent_installation__name", "target_region", "idempotency_key")
    readonly_fields = ("id", "idempotency_key", "created_at", "updated_at")


@admin.register(SearchRunExecutionAttempt)
class SearchRunExecutionAttemptAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "search_run", "attempt_number", "tool_execution", "created_at")
    list_filter = ("tenant",)
    search_fields = ("id", "search_run__id", "tool_execution__id")
    readonly_fields = ("id", "created_at")


@admin.register(SearchResult)
class SearchResultAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "search_run", "category", "created_at")
    list_filter = ("tenant",)
    search_fields = ("name", "address", "external_id", "dedupe_key")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Prospect)
class ProspectAdmin(admin.ModelAdmin):
    list_display = ("display_name", "tenant", "qualification_status", "priority", "external_id", "created_at")
    list_filter = ("tenant", "qualification_status", "priority")
    search_fields = ("display_name", "address", "phone", "website", "maps_url", "external_id", "identity_key")
    readonly_fields = ("id", "identity_key", "created_at", "updated_at")


@admin.register(ProspectSource)
class ProspectSourceAdmin(admin.ModelAdmin):
    list_display = ("prospect", "tenant", "search_result", "search_run", "created_at")
    list_filter = ("tenant",)
    search_fields = ("prospect__display_name", "search_result__name", "search_run__id")
    readonly_fields = ("id", "created_at")


@admin.register(ProspectActivity)
class ProspectActivityAdmin(admin.ModelAdmin):
    list_display = ("activity_type", "prospect", "contact", "occurred_at", "created_by", "tenant", "created_at")
    list_filter = ("tenant", "activity_type")
    search_fields = ("note", "prospect__display_name", "contact__name", "contact__email")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(ProspectContactOutcome)
class ProspectContactOutcomeAdmin(admin.ModelAdmin):
    list_display = ("outcome", "prospect", "contact", "occurred_at", "tenant", "recorded_by")
    list_filter = ("tenant", "outcome")
    search_fields = ("note", "prospect__display_name", "contact__name")
    readonly_fields = ("id", "idempotency_key", "created_at", "updated_at")


@admin.register(ProspectFollowUp)
class ProspectFollowUpAdmin(admin.ModelAdmin):
    list_display = ("action_type", "status", "due_at", "prospect", "contact", "tenant", "created_by")
    list_filter = ("tenant", "status", "action_type")
    search_fields = ("note", "prospect__display_name", "contact__name")
    readonly_fields = ("id", "idempotency_key", "completed_at", "created_at", "updated_at")


@admin.register(ProspectOutreachSend)
class ProspectOutreachSendAdmin(admin.ModelAdmin):
    list_display = ("destination", "status", "draft", "prospect", "tenant", "requested_by", "sent_at")
    list_filter = ("tenant", "status")
    search_fields = ("destination", "subject_snapshot", "prospect__display_name", "draft__subject")
    readonly_fields = ("id", "idempotency_key", "requested_at", "sent_at", "failed_at", "created_at", "updated_at")


@admin.register(ProspectOutreachDraft)
class ProspectOutreachDraftAdmin(admin.ModelAdmin):
    list_display = ("channel", "status", "prospect", "contact", "tenant", "created_by", "updated_at")
    list_filter = ("tenant", "channel", "status")
    search_fields = ("subject", "body", "prospect__display_name", "contact__name", "destination_email", "destination_phone")
    readonly_fields = ("id", "destination_email", "destination_phone", "prepared_at", "created_at", "updated_at")


@admin.register(ProspectContact)
class ProspectContactAdmin(admin.ModelAdmin):
    list_display = ("name", "role_title", "email", "phone", "tenant", "prospect", "source_type", "created_at")
    list_filter = ("tenant", "source_type")
    search_fields = ("name", "role_title", "email", "phone", "prospect__display_name")
    readonly_fields = ("id", "normalized_email", "normalized_phone", "created_at", "updated_at")


@admin.register(ProspectEnrichment)
class ProspectEnrichmentAdmin(admin.ModelAdmin):
    list_display = ("prospect", "tenant", "field", "value", "source_type", "observed_at")
    list_filter = ("tenant", "field", "source_type")
    search_fields = ("prospect__display_name", "value", "normalized_value", "source_reference", "source_url")
    readonly_fields = ("id", "normalized_value", "source_key", "created_at", "updated_at")
