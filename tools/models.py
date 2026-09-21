import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from agents.models import AgentInstallation
from projects.models import Project
from tenants.models import Tenant


SENSITIVE_KEY_FRAGMENTS = ("password", "token", "secret", "api_key", "apikey", "authorization", "private_key")


def validate_no_plaintext_secrets(value, field_name="payload"):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS) and item not in ("", None):
                raise ValidationError({field_name: "Tool data must not store plaintext secrets."})
            validate_no_plaintext_secrets(item, field_name=field_name)
    elif isinstance(value, list):
        for item in value:
            validate_no_plaintext_secrets(item, field_name=field_name)


class ToolDefinition(models.Model):
    class ExecutionMode(models.TextChoices):
        LOCAL = "LOCAL", "Local"
        DELEGATED = "DELEGATED", "Delegated"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.CharField(max_length=120, unique=True)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True, default="")
    category = models.CharField(max_length=80)
    runtime_handler = models.CharField(max_length=100, blank=True, default="")
    execution_mode = models.CharField(max_length=16, choices=ExecutionMode.choices, default=ExecutionMode.LOCAL)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "slug"]

    def clean(self):
        super().clean()
        if self.execution_mode == self.ExecutionMode.LOCAL and not self.runtime_handler:
            raise ValidationError({"runtime_handler": "Local tools require a runtime handler."})

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
        validate_no_plaintext_secrets(self.configuration, field_name="configuration")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.agent_installation} / {self.tool_definition.slug}"


class ToolExecutor(models.Model):
    class ExecutorType(models.TextChoices):
        BROWSER_EXTENSION = "BROWSER_EXTENSION", "Browser extension"
        SMART_SALES = "SMART_SALES", "Smart Sales"
        WORKER = "WORKER", "Worker"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="tool_executors", null=True, blank=True)
    name = models.CharField(max_length=160)
    executor_type = models.CharField(max_length=32, choices=ExecutorType.choices)
    public_id = models.CharField(max_length=120, unique=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "public_id"]
        indexes = [models.Index(fields=["tenant", "is_active"]), models.Index(fields=["public_id", "is_active"])]

    def __str__(self):
        return self.name


class ToolExecutorCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    executor = models.ForeignKey(ToolExecutor, on_delete=models.CASCADE, related_name="credentials")
    credential_prefix = models.CharField(max_length=32, unique=True)
    secret_hash = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["credential_prefix"]),
            models.Index(fields=["executor", "is_active"]),
            models.Index(fields=["expires_at"]),
        ]

    @property
    def is_usable(self):
        if not self.is_active or self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()

    def __str__(self):
        return f"{self.executor} / {self.credential_prefix}"


class ToolExecutorPairingRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        EXPIRED = "EXPIRED", "Expired"
        CONSUMED = "CONSUMED", "Consumed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="tool_executor_pairings", null=True, blank=True)
    executor_type = models.CharField(max_length=32, choices=ToolExecutor.ExecutorType.choices)
    requested_name = models.CharField(max_length=160)
    pairing_code = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="approved_tool_executor_pairings", null=True, blank=True
    )
    executor = models.ForeignKey(ToolExecutor, on_delete=models.SET_NULL, related_name="pairing_requests", null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["pairing_code"]),
            models.Index(fields=["status"]),
            models.Index(fields=["expires_at"]),
        ]

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()

    def __str__(self):
        return f"{self.requested_name} / {self.status}"


class ToolExecutorCapability(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    executor = models.ForeignKey(ToolExecutor, on_delete=models.CASCADE, related_name="capabilities")
    tool_definition = models.ForeignKey(ToolDefinition, on_delete=models.CASCADE, related_name="executor_capabilities")
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["executor__name", "tool_definition__slug"]
        constraints = [
            models.UniqueConstraint(fields=["executor", "tool_definition"], name="unique_tool_executor_capability"),
        ]
        indexes = [models.Index(fields=["executor", "is_enabled"]), models.Index(fields=["tool_definition", "is_enabled"])]

    def __str__(self):
        return f"{self.executor} / {self.tool_definition.slug}"


class ToolExecution(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DISPATCHED = "DISPATCHED", "Dispatched"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="tool_executions")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tool_executions")
    agent_installation = models.ForeignKey(AgentInstallation, on_delete=models.CASCADE, related_name="tool_executions")
    tool_binding = models.ForeignKey(AgentToolBinding, on_delete=models.PROTECT, related_name="executions")
    tool_definition = models.ForeignKey(ToolDefinition, on_delete=models.PROTECT, related_name="executions")
    executor = models.ForeignKey(ToolExecutor, on_delete=models.PROTECT, related_name="executions", null=True, blank=True)
    execution_mode = models.CharField(max_length=16, choices=ToolDefinition.ExecutionMode.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    request_payload = models.JSONField(default=dict, blank=True)
    result_payload = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=80, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    idempotency_key = models.CharField(max_length=160, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "agent_installation", "tool_definition", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="unique_tool_execution_idempotency_context",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["project", "status"]),
            models.Index(fields=["tool_definition", "status"]),
            models.Index(fields=["executor", "status"]),
            models.Index(fields=["execution_mode", "status"]),
        ]

    def clean(self):
        super().clean()
        if self.agent_installation_id and self.tenant_id and self.agent_installation.tenant_id != self.tenant_id:
            raise ValidationError({"tenant": "Execution tenant must match the agent installation tenant."})
        if self.agent_installation_id and self.project_id and self.agent_installation.project_id != self.project_id:
            raise ValidationError({"project": "Execution project must match the agent installation project."})
        if self.tool_binding_id:
            if self.tool_binding.agent_installation_id != self.agent_installation_id:
                raise ValidationError({"tool_binding": "Execution binding must match the agent installation."})
            if self.tool_binding.tool_definition_id != self.tool_definition_id:
                raise ValidationError({"tool_binding": "Execution binding must match the tool definition."})
        if self.tool_definition_id and self.execution_mode != self.tool_definition.execution_mode:
            raise ValidationError({"execution_mode": "Execution mode must match the tool definition."})
        validate_no_plaintext_secrets(self.request_payload, field_name="request_payload")
        validate_no_plaintext_secrets(self.result_payload, field_name="result_payload")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.tool_definition.slug} / {self.status}"
