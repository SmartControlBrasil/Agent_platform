import uuid

from django.core.exceptions import ValidationError
from django.db import models

from agents.models import AgentInstallation
from projects.models import Project
from tenants.models import Tenant


class ToolDefinition(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.CharField(max_length=120, unique=True)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True, default="")
    category = models.CharField(max_length=80)
    runtime_handler = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "slug"]

    def __str__(self):
        return self.slug


class AgentToolBinding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="agent_tool_bindings")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="agent_tool_bindings")
    agent_installation = models.ForeignKey(
        AgentInstallation, on_delete=models.CASCADE, related_name="tool_bindings"
    )
    tool_definition = models.ForeignKey(ToolDefinition, on_delete=models.PROTECT, related_name="agent_bindings")
    is_enabled = models.BooleanField(default=True)
    configuration = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant__name", "project__name", "tool_definition__slug"]
        constraints = [
            models.UniqueConstraint(
                fields=["agent_installation", "tool_definition"], name="unique_tool_binding_per_installation"
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_enabled"]),
            models.Index(fields=["project", "is_enabled"]),
            models.Index(fields=["agent_installation", "is_enabled"]),
        ]

    def clean(self):
        super().clean()
        if (
            self.agent_installation_id
            and self.tenant_id
            and self.agent_installation.tenant_id != self.tenant_id
        ):
            raise ValidationError({"tenant": "Binding tenant must match the agent installation tenant."})
        if (
            self.agent_installation_id
            and self.project_id
            and self.agent_installation.project_id != self.project_id
        ):
            raise ValidationError({"project": "Binding project must match the agent installation project."})
        self._validate_configuration_has_no_plaintext_secrets()

    def _validate_configuration_has_no_plaintext_secrets(self):
        if not isinstance(self.configuration, dict):
            return
        blocked = {"secret", "token", "password", "api_key", "apikey", "private_key"}
        for key, value in self.configuration.items():
            normalized = str(key).lower()
            if any(marker in normalized for marker in blocked) and value not in ("", None):
                raise ValidationError({"configuration": "Tool binding configuration must not store plaintext secrets."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.agent_installation} / {self.tool_definition.slug}"
