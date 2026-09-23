from django.contrib import admin

from .models import AgentToolBinding, ServiceClient, ServiceClientAgentAccess, ServiceClientCredential, ToolDefinition, ToolExecution, ToolExecutor, ToolExecutorCapability


@admin.register(ToolDefinition)
class ToolDefinitionAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "category", "execution_mode", "runtime_handler", "is_active")
    list_filter = ("category", "execution_mode", "is_active")
    search_fields = ("slug", "name", "runtime_handler")


@admin.register(AgentToolBinding)
class AgentToolBindingAdmin(admin.ModelAdmin):
    list_display = ("tool_definition", "agent_installation", "tenant", "project", "is_enabled")
    list_filter = ("is_enabled", "tool_definition__category")
    search_fields = ("tool_definition__slug", "agent_installation__name", "tenant__name", "project__name")


@admin.register(ToolExecution)
class ToolExecutionAdmin(admin.ModelAdmin):
    list_display = ("tool_definition", "agent_installation", "execution_mode", "status", "executor", "created_at")
    list_filter = ("execution_mode", "status", "tool_definition__category")
    search_fields = ("tool_definition__slug", "agent_installation__name", "executor__name", "idempotency_key")
    readonly_fields = ("request_payload", "result_payload")


@admin.register(ToolExecutor)
class ToolExecutorAdmin(admin.ModelAdmin):
    list_display = ("name", "executor_type", "public_id", "tenant", "is_active", "last_seen_at")
    list_filter = ("executor_type", "is_active")
    search_fields = ("name", "public_id", "tenant__name")


@admin.register(ToolExecutorCapability)
class ToolExecutorCapabilityAdmin(admin.ModelAdmin):
    list_display = ("executor", "tool_definition", "is_enabled")
    list_filter = ("is_enabled", "tool_definition__category")
    search_fields = ("executor__name", "tool_definition__slug")


@admin.register(ServiceClient)
class ServiceClientAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "tenant", "is_active", "created_at")
    list_filter = ("is_active", "tenant")
    search_fields = ("name", "slug", "tenant__name")


@admin.register(ServiceClientCredential)
class ServiceClientCredentialAdmin(admin.ModelAdmin):
    list_display = ("service_client", "credential_prefix", "is_active", "last_used_at", "expires_at")
    list_filter = ("is_active",)
    search_fields = ("service_client__name", "credential_prefix")
    readonly_fields = ("secret_hash",)


@admin.register(ServiceClientAgentAccess)
class ServiceClientAgentAccessAdmin(admin.ModelAdmin):
    list_display = ("service_client", "agent_installation", "tenant", "can_execute", "is_active")
    list_filter = ("can_execute", "is_active")
    search_fields = ("service_client__name", "agent_installation__name", "tenant__name")
