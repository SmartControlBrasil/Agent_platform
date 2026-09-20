import uuid

from django.core.exceptions import ValidationError
from django.db import models

from projects.models import Project
from tenants.models import Tenant


class AgentDefinition(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    agent_type = models.CharField(max_length=80)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.slug


class AgentVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        DEPRECATED = "deprecated", "Deprecated"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent_definition = models.ForeignKey(AgentDefinition, on_delete=models.CASCADE, related_name="versions")
    version = models.CharField(max_length=40)
    runtime_handler = models.CharField(max_length=80)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["agent_definition__slug", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["agent_definition", "version"], name="unique_agent_version_per_definition"),
        ]

    def __str__(self):
        return f"{self.agent_definition.slug}@{self.version}"


class AgentInstallation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="agent_installations")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="agent_installations")
    agent_definition = models.ForeignKey(AgentDefinition, on_delete=models.PROTECT, related_name="installations")
    agent_version = models.ForeignKey(AgentVersion, on_delete=models.PROTECT, related_name="installations")
    name = models.CharField(max_length=120)
    is_enabled = models.BooleanField(default=True)
    configuration = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant__name", "project__name", "name"]
        indexes = [
            models.Index(fields=["tenant", "is_enabled"]),
            models.Index(fields=["project", "is_enabled"]),
        ]

    def clean(self):
        super().clean()
        if self.project_id and self.tenant_id and self.project.tenant_id != self.tenant_id:
            raise ValidationError({"project": "Project must belong to the same tenant as the installation."})
        if (
            self.agent_version_id
            and self.agent_definition_id
            and self.agent_version.agent_definition_id != self.agent_definition_id
        ):
            raise ValidationError({"agent_version": "AgentVersion must belong to the selected AgentDefinition."})
        self._validate_configuration_has_no_plaintext_secrets()
        self._validate_agent_specific_configuration()

    def _validate_configuration_has_no_plaintext_secrets(self):
        if not isinstance(self.configuration, dict):
            return
        blocked = {"secret", "token", "password", "api_key", "apikey", "private_key"}
        for key, value in self.configuration.items():
            normalized = str(key).lower()
            if any(marker in normalized for marker in blocked) and value not in ("", None):
                raise ValidationError({"configuration": "Configuration must not store plaintext secrets."})

    def _validate_agent_specific_configuration(self):
        if not self.agent_definition_id:
            return
        from agents.infrastructure.configuration_validation import validate_agent_configuration

        validate_agent_configuration(agent_slug=self.agent_definition.slug, configuration=self.configuration)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.tenant.slug} / {self.project.slug} / {self.name}"
