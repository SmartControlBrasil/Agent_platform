from django.contrib import admin

from .models import AgentDefinition, AgentInstallation, AgentVersion


class AgentVersionInline(admin.TabularInline):
    model = AgentVersion
    extra = 0
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(AgentDefinition)
class AgentDefinitionAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "agent_type", "is_active")
    search_fields = ("slug", "name", "agent_type")
    inlines = [AgentVersionInline]
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(AgentInstallation)
class AgentInstallationAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "project", "agent_definition", "agent_version", "is_enabled")
    list_filter = ("is_enabled", "agent_definition", "tenant")
    search_fields = ("name", "tenant__name", "tenant__slug", "project__name", "project__slug")
    readonly_fields = ("id", "created_at", "updated_at")
