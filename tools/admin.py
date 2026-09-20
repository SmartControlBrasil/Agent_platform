from django.contrib import admin

from .models import AgentToolBinding, ToolDefinition


@admin.register(ToolDefinition)
class ToolDefinitionAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "category", "runtime_handler", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("slug", "name", "runtime_handler")


@admin.register(AgentToolBinding)
class AgentToolBindingAdmin(admin.ModelAdmin):
    list_display = ("tool_definition", "agent_installation", "tenant", "project", "is_enabled")
    list_filter = ("is_enabled", "tool_definition__category")
    search_fields = ("tool_definition__slug", "agent_installation__name", "tenant__name", "project__name")
