import math

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from tenants.models import Tenant
from knowledge_base.rag.vector_field import RagVectorField, configured_embedding_dimensions


class TenantRagConfiguration(models.Model):
    SOURCE_MANUAL = "manual"
    SOURCE_GOOGLE_DRIVE = "google_drive"

    class SourceMode(models.TextChoices):
        MANUAL = "manual", "Manual/local"
        GOOGLE_DRIVE = "google_drive", "Google Drive"

    class InventoryStatus(models.TextChoices):
        IDLE = "idle", "Idle"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    tenant = models.OneToOneField(
        Tenant,
        on_delete=models.CASCADE,
        related_name="rag_configuration",
    )
    source_mode = models.CharField(
        max_length=30,
        choices=SourceMode.choices,
        default=SOURCE_MANUAL,
        help_text="Origem operacional do conhecimento: manual/local ou Google Drive.",
    )
    approved_folder_id = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text="ID da pasta aprovada no Google Drive. Opcional para conhecimento manual/local.",
    )
    sync_enabled = models.BooleanField(default=False)
    retrieval_enabled = models.BooleanField(
        default=False,
        help_text="Permite recuperação semântica no fluxo de conversa deste tenant.",
    )
    operational_monitoring_enabled = models.BooleanField(
        default=False,
        help_text="Habilita monitoramento automático de alertas operacionais para este tenant.",
    )
    min_similarity_score = models.FloatField(
        null=True,
        blank=True,
        help_text="Override por tenant para threshold de similaridade (0.0 a 1.0).",
    )
    max_retrieved_chunks = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Limite máximo de chunks recuperados no chat (null = default global).",
    )
    max_context_chars = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Orçamento máximo de caracteres de contexto RAG no chat (null = default global).",
    )
    retrieval_timeout_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Timeout da recuperação/embed da query no chat (null = default global).",
    )
    last_inventory_status = models.CharField(
        max_length=20,
        choices=InventoryStatus.choices,
        default=InventoryStatus.IDLE,
    )
    last_inventory_at = models.DateTimeField(null=True, blank=True)
    last_inventory_started_at = models.DateTimeField(null=True, blank=True)
    last_inventory_mode = models.CharField(max_length=30, blank=True)
    last_inventory_file_count = models.PositiveIntegerField(default=0)
    last_inventory_error = models.CharField(max_length=500, blank=True)
    last_index_status = models.CharField(
        max_length=20,
        choices=InventoryStatus.choices,
        default=InventoryStatus.IDLE,
    )
    last_index_at = models.DateTimeField(null=True, blank=True)
    last_index_started_at = models.DateTimeField(null=True, blank=True)
    last_index_mode = models.CharField(max_length=30, blank=True)
    last_index_run_id = models.CharField(max_length=64, blank=True)
    last_index_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant__slug"]
        indexes = [
            models.Index(fields=["sync_enabled"]),
            models.Index(fields=["retrieval_enabled"]),
            models.Index(fields=["source_mode"]),
            models.Index(fields=["operational_monitoring_enabled"]),
            models.Index(fields=["last_inventory_status"]),
            models.Index(fields=["last_index_status"]),
        ]

    def __str__(self):
        source = self.approved_folder_id or self.source_mode
        return f"{self.tenant.slug} / {source}"

    @property
    def uses_google_drive(self) -> bool:
        return self.source_mode == self.SOURCE_GOOGLE_DRIVE

    @property
    def requires_drive_folder(self) -> bool:
        return self.uses_google_drive and self.sync_enabled

    def _normalize_source_mode(self):
        self.approved_folder_id = str(self.approved_folder_id or "").strip()
        if self.source_mode not in {self.SOURCE_MANUAL, self.SOURCE_GOOGLE_DRIVE}:
            self.source_mode = self.SOURCE_MANUAL
        if self.source_mode == self.SOURCE_MANUAL and self.sync_enabled and self.approved_folder_id:
            self.source_mode = self.SOURCE_GOOGLE_DRIVE

    def clean(self):
        self._normalize_source_mode()
        errors = {}
        if self.source_mode == self.SOURCE_GOOGLE_DRIVE and self.sync_enabled and not self.approved_folder_id:
            errors["approved_folder_id"] = "Google Drive sync requires an approved folder ID."
        if self.source_mode == self.SOURCE_MANUAL and self.sync_enabled:
            errors["sync_enabled"] = "Manual/local knowledge does not use Google Drive sync."
        if self.min_similarity_score is not None:
            value = float(self.min_similarity_score)
            if not math.isfinite(value) or value < 0.0 or value > 1.0:
                errors["min_similarity_score"] = "Threshold must be a finite number between 0 and 1."
        if self.max_retrieved_chunks is not None and int(self.max_retrieved_chunks) <= 0:
            errors["max_retrieved_chunks"] = "Must be a positive integer when set."
        if self.max_context_chars is not None and int(self.max_context_chars) <= 0:
            errors["max_context_chars"] = "Must be a positive integer when set."
        if self.retrieval_timeout_seconds is not None and int(self.retrieval_timeout_seconds) <= 0:
            errors["retrieval_timeout_seconds"] = "Must be a positive integer when set."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            fields.update({"source_mode", "approved_folder_id"})
            kwargs["update_fields"] = list(fields)
        super().save(*args, **kwargs)


class TenantRagDriveFileManifest(models.Model):
    class Status(models.TextChoices):
        DISCOVERED = "discovered", "Discovered"
        EXPORTED = "exported", "Exported"
        UPDATED = "updated", "Updated"
        UNCHANGED = "unchanged", "Unchanged"
        SKIPPED_UNSUPPORTED = "skipped_unsupported", "Skipped unsupported"
        SKIPPED_TEMPORARY_ARTIFACT = "skipped_temporary_artifact", "Skipped temporary artifact"
        FAILED = "failed", "Failed"
        REMOVED = "removed", "Removed"
        UNAVAILABLE = "unavailable", "Unavailable"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="rag_drive_file_manifests",
    )
    configuration = models.ForeignKey(
        TenantRagConfiguration,
        on_delete=models.CASCADE,
        related_name="drive_file_manifests",
    )
    drive_file_id = models.CharField(max_length=120)
    name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=160)
    relative_path = models.CharField(max_length=600, blank=True)
    drive_modified_time = models.DateTimeField(null=True, blank=True)
    drive_size_bytes = models.BigIntegerField(null=True, blank=True)
    normalized_text_sha256 = models.CharField(max_length=64, blank=True)
    document_metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DISCOVERED)
    is_active = models.BooleanField(default=True)
    first_discovered_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)
    last_exported_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant__slug", "relative_path", "name", "drive_file_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "drive_file_id"],
                name="unique_rag_drive_file_per_tenant",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["tenant", "last_seen_at"]),
            models.Index(fields=["tenant", "updated_at"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / {self.drive_file_id} / {self.status}"


class TenantRagDriveTextStaging(models.Model):
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="rag_drive_text_staging",
    )
    manifest = models.OneToOneField(
        TenantRagDriveFileManifest,
        on_delete=models.CASCADE,
        related_name="text_staging",
    )
    normalized_text = models.TextField(blank=True)
    normalized_text_sha256 = models.CharField(max_length=64)
    normalized_text_char_count = models.PositiveIntegerField(default=0)
    normalized_text_byte_count = models.PositiveIntegerField(default=0)
    exported_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant__slug", "-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "updated_at"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / {self.manifest.drive_file_id}"


class TenantRagDocumentChunk(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REPLACED = "replaced", "Replaced"
        FAILED = "failed", "Failed"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="rag_document_chunks",
    )
    manifest = models.ForeignKey(
        TenantRagDriveFileManifest,
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    staging = models.ForeignKey(
        TenantRagDriveTextStaging,
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    ordinal = models.PositiveIntegerField()
    chunk_text = models.TextField(blank=True)
    chunk_sha256 = models.CharField(max_length=64)
    source_text_sha256 = models.CharField(max_length=64)
    chunk_config_signature = models.CharField(max_length=64)
    char_count = models.PositiveIntegerField(default=0)
    byte_count = models.PositiveIntegerField(default=0)
    start_char = models.PositiveIntegerField(default=0)
    end_char = models.PositiveIntegerField(default=0)
    chunk_metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant__slug", "manifest_id", "ordinal"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "manifest", "source_text_sha256", "chunk_config_signature", "ordinal"],
                name="unique_rag_chunk_per_document_version_ordinal",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "manifest", "is_active"]),
            models.Index(fields=["tenant", "source_text_sha256"]),
            models.Index(fields=["tenant", "status"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / {self.manifest.drive_file_id} / {self.ordinal}"

    def clean(self):
        from django.core.exceptions import ValidationError

        errors = {}
        if self.manifest_id and self.tenant_id and self.manifest.tenant_id != self.tenant_id:
            errors["manifest"] = "Manifest tenant must match chunk tenant."
        if self.staging_id and self.tenant_id and self.staging.tenant_id != self.tenant_id:
            errors["staging"] = "Staging tenant must match chunk tenant."
        if self.manifest_id and self.staging_id and self.staging.manifest_id != self.manifest_id:
            errors["staging"] = "Staging must belong to manifest."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class TenantRagChunkEmbedding(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REPLACED = "replaced", "Replaced"
        FAILED = "failed", "Failed"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="rag_chunk_embeddings",
    )
    chunk = models.ForeignKey(
        TenantRagDocumentChunk,
        on_delete=models.CASCADE,
        related_name="embeddings",
    )
    manifest = models.ForeignKey(
        TenantRagDriveFileManifest,
        on_delete=models.CASCADE,
        related_name="chunk_embeddings",
    )
    chunk_sha256 = models.CharField(max_length=64)
    chunk_config_signature = models.CharField(max_length=64)
    provider = models.CharField(max_length=40)
    model = models.CharField(max_length=120)
    dimension = models.PositiveIntegerField()
    embedding_config_signature = models.CharField(max_length=64)
    vector = RagVectorField(dimensions=configured_embedding_dimensions(), default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    is_active = models.BooleanField(default=True)
    first_indexed_at = models.DateTimeField(null=True, blank=True)
    last_indexed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant__slug", "chunk_id", "-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "chunk", "embedding_config_signature"],
                name="unique_rag_embedding_per_chunk_config",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "is_active", "status"]),
            models.Index(fields=["tenant", "embedding_config_signature"]),
            models.Index(fields=["tenant", "chunk", "is_active"]),
            models.Index(fields=["tenant", "manifest"]),
            models.Index(fields=["tenant", "provider", "model", "dimension", "is_active"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / chunk={self.chunk_id} / {self.model}"

    def clean(self):
        from django.core.exceptions import ValidationError

        errors = {}
        if self.chunk_id and self.tenant_id and self.chunk.tenant_id != self.tenant_id:
            errors["chunk"] = "Chunk tenant must match embedding tenant."
        if self.manifest_id and self.tenant_id and self.manifest.tenant_id != self.tenant_id:
            errors["manifest"] = "Manifest tenant must match embedding tenant."
        if self.chunk_id and self.manifest_id and self.chunk.manifest_id != self.manifest_id:
            errors["manifest"] = "Manifest must belong to chunk."
        expected_dim = configured_embedding_dimensions()
        if self.dimension and int(self.dimension) != expected_dim:
            # Permite embeddings históricos de outra dimensão, mas impede silent mismatch na config atual.
            pass
        if self.vector is not None and self.dimension and len(self.vector) != int(self.dimension):
            errors["vector"] = "Vector length must match embedding dimension."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class TenantRagIndexRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="rag_index_runs",
    )
    run_id = models.CharField(max_length=64)
    mode = models.CharField(max_length=30)
    provider = models.CharField(max_length=40, blank=True)
    model = models.CharField(max_length=120, blank=True)
    dimension = models.PositiveIntegerField(default=0)
    embedding_config_signature = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    dry_run = models.BooleanField(default=False)
    documents = models.PositiveIntegerField(default=0)
    chunks = models.PositiveIntegerField(default=0)
    pending = models.PositiveIntegerField(default=0)
    indexed = models.PositiveIntegerField(default=0)
    reindexed = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    deactivated = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    batches = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "run_id"],
                name="unique_rag_index_run_per_tenant",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "started_at"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / {self.run_id} / {self.status}"


class TenantRagOperationRequest(models.Model):
    """Solicitação operacional de sync/index RAG consumida por worker controlado."""

    class Operation(models.TextChoices):
        INVENTORY = "inventory", "Inventário da origem"
        SYNC_EXPORT = "sync_export", "Sincronização de documentos"
        BUILD_CHUNKS = "build_chunks", "Atualização de chunks"
        INDEX_EMBEDDINGS = "index_embeddings", "Geração de embeddings pendentes"
        FULL_REINDEX = "full_reindex", "Reindexação completa"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="rag_operation_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_operation_requests",
    )
    operation = models.CharField(max_length=40, choices=Operation.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    dry_run = models.BooleanField(default=False)
    run_id = models.CharField(max_length=64)
    counters = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=500, blank=True)
    index_run = models.ForeignKey(
        TenantRagIndexRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operation_requests",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "run_id"],
                name="unique_rag_operation_run_per_tenant",
            ),
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(
                    status__in=[
                        "pending",
                        "running",
                    ]
                ),
                name="unique_active_rag_operation_per_tenant",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status", "created_at"]),
            models.Index(fields=["tenant", "operation", "created_at"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / {self.operation} / {self.status}"


class RagRetrievalEvent(models.Model):
    """Metrica operacional de retrieval (nao e audit log e nao guarda conteudo)."""

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        EMPTY = "empty", "Empty"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="rag_retrieval_events",
    )
    conversation_id = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    reason = models.CharField(max_length=80, blank=True)
    backend = models.CharField(max_length=40, blank=True)
    provider = models.CharField(max_length=40, blank=True)
    model = models.CharField(max_length=120, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    candidate_count = models.PositiveIntegerField(default=0)
    result_count = models.PositiveIntegerField(default=0)
    max_score = models.FloatField(default=0.0)
    threshold = models.FloatField(default=0.0)
    threshold_source = models.CharField(max_length=30, blank=True, default="global_default")
    dry_run = models.BooleanField(default=False)
    hit = models.BooleanField(default=False)
    retrieval_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "created_at"]),
            models.Index(fields=["tenant", "status", "created_at"]),
            models.Index(fields=["tenant", "hit", "created_at"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / {self.status} / hit={self.hit}"


class KnowledgeDocument(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DRAFT = "draft", "Draft"
        ARCHIVED = "archived", "Archived"

    class LifecycleStatus(models.TextChoices):
        NEW = "new", "New"
        IMPORTED = "imported", "Imported"
        INDEXING = "indexing", "Indexing"
        INDEXED = "indexed", "Indexed"
        STALE = "stale", "Stale"
        FAILED = "failed", "Failed"
        DISABLED = "disabled", "Disabled"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="knowledge_documents",
    )

    title = models.CharField(max_length=180)
    slug = models.SlugField(max_length=120)
    content = models.TextField(blank=True)
    source_type = models.CharField(max_length=40, default="manual")
    source_url = models.URLField(blank=True)
    tags = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    content_sha256 = models.CharField(max_length=64, blank=True)
    indexed_content_sha256 = models.CharField(max_length=64, blank=True)
    lifecycle_status = models.CharField(
        max_length=20,
        choices=LifecycleStatus.choices,
        default=LifecycleStatus.NEW,
    )
    last_indexed_at = models.DateTimeField(null=True, blank=True)
    last_index_error = models.CharField(max_length=500, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "slug"],
                name="unique_knowledge_document_per_tenant_slug",
            )
        ]

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    def __str__(self):
        return f"{self.title} / {self.tenant.slug}"


class TenantOperationalAlert(models.Model):
    """Alerta operacional persistente tenant-scoped (RAG/IA)."""

    class Category(models.TextChoices):
        CONFIGURATION = "configuration", "Configuration"
        ENVIRONMENT = "environment", "Environment"
        DATABASE = "database", "Database"
        VECTOR_HEALTH = "vector_health", "Vector health"
        RAG_OPERATIONS = "rag_operations", "RAG operations"
        RETRIEVAL = "retrieval", "Retrieval"
        GROUNDED_AI = "grounded_ai", "Grounded AI"
        OPENAI_PROVIDER = "openai_provider", "OpenAI provider"
        TOKEN_USAGE = "token_usage", "Token usage"
        TENANT_ISOLATION = "tenant_isolation", "Tenant isolation"
        INTEGRATION_SAFETY = "integration_safety", "Integration safety"

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        RESOLVED = "resolved", "Resolved"

    class ResolutionSource(models.TextChoices):
        AUTO = "auto", "Automatic"
        MANUAL = "manual", "Manual"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="operational_alerts",
    )
    category = models.CharField(max_length=40, choices=Category.choices)
    severity = models.CharField(max_length=20, choices=Severity.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    rule_id = models.CharField(max_length=80)
    fingerprint = models.CharField(max_length=160)
    title = models.CharField(max_length=200)
    summary = models.CharField(max_length=500)
    detected_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    occurrence_count = models.PositiveIntegerField(default=1)
    source = models.CharField(max_length=80, default="operational_alert_sync")
    source_reference = models.CharField(max_length=120, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acknowledged_operational_alerts",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_operational_alerts",
    )
    resolution_note = models.CharField(max_length=500, blank=True)
    resolution_source = models.CharField(
        max_length=20,
        choices=ResolutionSource.choices,
        blank=True,
    )
    assigned_to = models.ForeignKey(
        "tenants.TenantMembership",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_operational_alerts",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_operational_alerts",
    )
    assigned_at = models.DateTimeField(null=True, blank=True)
    ack_due_at = models.DateTimeField(null=True, blank=True)
    resolution_due_at = models.DateTimeField(null=True, blank=True)
    last_sync_batch_id = models.CharField(max_length=64, blank=True)
    reopen_count = models.PositiveIntegerField(default=0)
    last_reopened_at = models.DateTimeField(null=True, blank=True)
    escalation_level = models.PositiveSmallIntegerField(default=0)
    escalated_at = models.DateTimeField(null=True, blank=True)
    escalation_trigger = models.CharField(max_length=40, blank=True)
    escalation_reason = models.CharField(max_length=500, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "fingerprint"],
                name="unique_operational_alert_fingerprint_per_tenant",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "severity"]),
            models.Index(fields=["tenant", "category"]),
            models.Index(fields=["tenant", "last_seen_at"]),
            models.Index(fields=["tenant", "fingerprint"]),
            models.Index(fields=["tenant", "assigned_to"]),
            models.Index(fields=["tenant", "ack_due_at"]),
            models.Index(fields=["tenant", "resolution_due_at"]),
            models.Index(fields=["tenant", "escalation_level"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / {self.rule_id} / {self.status}"


class TenantOperationalAlertSilence(models.Model):
    """Silenciamento temporário de alerta operacional."""

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="operational_alert_silences",
    )
    alert = models.ForeignKey(
        TenantOperationalAlert,
        on_delete=models.CASCADE,
        related_name="silences",
    )
    reason = models.CharField(max_length=500)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_operational_alert_silences",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_operational_alert_silences",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at", "-id"]
        indexes = [
            models.Index(fields=["tenant", "alert", "ends_at"]),
            models.Index(fields=["tenant", "starts_at", "ends_at"]),
        ]

    def __str__(self):
        return f"silence/{self.alert_id}/{self.starts_at}"


class TenantOperationalMaintenanceWindow(models.Model):
    """Janela de manutenção programada tenant-scoped."""

    class Scope(models.TextChoices):
        ALL = "all", "All operational rules"
        CATEGORIES = "categories", "Specific categories"
        RULES = "rules", "Specific rule ids"
        RESOURCE = "resource", "Specific resource reference"

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        ACTIVE = "active", "Active"
        ENDED = "ended", "Ended"
        CANCELLED = "cancelled", "Cancelled"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="operational_maintenance_windows",
    )
    title = models.CharField(max_length=200)
    description = models.CharField(max_length=500)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.ALL)
    scope_categories = models.JSONField(default=list, blank=True)
    scope_rule_ids = models.JSONField(default=list, blank=True)
    scope_resource_reference = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_operational_maintenance_windows",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_operational_maintenance_windows",
    )
    cancellation_note = models.CharField(max_length=500, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-starts_at", "-id"]
        indexes = [
            models.Index(fields=["tenant", "status", "starts_at"]),
            models.Index(fields=["tenant", "starts_at", "ends_at"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / {self.title} / {self.status}"


class OperationalMonitoringBatchRun(models.Model):
    """Execução global de monitoramento operacional (lock + agregados)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    class Trigger(models.TextChoices):
        PORTAL = "portal", "Portal"
        CLI = "cli", "CLI"
        SCHEDULER = "scheduler", "Scheduler"
        TEST = "test", "Test"

    trigger = models.CharField(max_length=20, choices=Trigger.choices, default=Trigger.CLI)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    dry_run = models.BooleanField(default=True)
    period = models.CharField(max_length=10, default="7d")
    worker_identifier = models.CharField(max_length=120, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    tenants_total = models.PositiveIntegerField(default=0)
    tenants_processed = models.PositiveIntegerField(default=0)
    tenants_failed = models.PositiveIntegerField(default=0)
    tenants_skipped = models.PositiveIntegerField(default=0)
    alerts_created = models.PositiveIntegerField(default=0)
    alerts_updated = models.PositiveIntegerField(default=0)
    alerts_resolved = models.PositiveIntegerField(default=0)
    alerts_reopened = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    error_category = models.CharField(max_length=40, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["status", "started_at"]),
            models.Index(fields=["trigger", "started_at"]),
        ]

    def __str__(self):
        return f"monitoring-batch/{self.pk}/{self.status}"


class TenantOperationalMonitoringRun(models.Model):
    """Execução de monitoramento por tenant."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    batch = models.ForeignKey(
        OperationalMonitoringBatchRun,
        on_delete=models.CASCADE,
        related_name="tenant_runs",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="operational_monitoring_runs",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    alerts_created = models.PositiveIntegerField(default=0)
    alerts_updated = models.PositiveIntegerField(default=0)
    alerts_resolved = models.PositiveIntegerField(default=0)
    alerts_reopened = models.PositiveIntegerField(default=0)
    alerts_active = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    error_category = models.CharField(max_length=40, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["tenant", "status", "started_at"]),
            models.Index(fields=["batch", "tenant"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / monitoring / {self.status}"


class TenantOperationalNotificationPreference(models.Model):
    """Preferências de notificação operacional por membership."""

    class DigestFrequency(models.TextChoices):
        IMMEDIATE = "immediate", "Immediate"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="operational_notification_preferences",
    )
    membership = models.OneToOneField(
        "tenants.TenantMembership",
        on_delete=models.CASCADE,
        related_name="operational_notification_preference",
    )
    in_app_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=False)
    notify_on_assignment = models.BooleanField(default=True)
    notify_on_escalation = models.BooleanField(default=True)
    notify_on_sla_breach = models.BooleanField(default=True)
    notify_on_resolution = models.BooleanField(default=True)
    digest_frequency = models.CharField(
        max_length=20,
        choices=DigestFrequency.choices,
        default=DigestFrequency.IMMEDIATE,
    )
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default="America/Sao_Paulo")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "membership"],
                name="unique_operational_notification_pref_per_membership",
            )
        ]

    def __str__(self):
        return f"notif-pref/{self.tenant.slug}/{self.membership_id}"


class TenantOperationalNotification(models.Model):
    """Notificação operacional tenant-scoped (outbox + in-app)."""

    class Channel(models.TextChoices):
        IN_APP = "in_app", "In-app"
        EMAIL = "email", "Email"
        WEBHOOK = "webhook", "Webhook"

    class Category(models.TextChoices):
        ALERT = "alert", "Alert"
        SLA = "sla", "SLA"
        ESCALATION = "escalation", "Escalation"
        OWNERSHIP = "ownership", "Ownership"
        MAINTENANCE = "maintenance", "Maintenance"
        MONITORING = "monitoring", "Monitoring"
        DIGEST = "digest", "Digest"

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        SUPPRESSED = "suppressed", "Suppressed"

    class SourceType(models.TextChoices):
        OPERATIONAL_ALERT = "operational_alert", "Operational alert"
        MAINTENANCE_WINDOW = "maintenance_window", "Maintenance window"
        MONITORING_RUN = "monitoring_run", "Monitoring run"
        DIGEST = "digest", "Digest"

    class DestinationRoute(models.TextChoices):
        ALERT_DETAIL = "alert_detail", "Alert detail"
        WORK_QUEUE = "work_queue", "Work queue"
        MY_WORK = "my_work", "My work"
        MAINTENANCE = "maintenance", "Maintenance"
        HEALTH = "health", "Health"
        NOTIFICATIONS = "notifications", "Notifications"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="operational_notifications",
    )
    recipient_membership = models.ForeignKey(
        "tenants.TenantMembership",
        on_delete=models.CASCADE,
        related_name="operational_notifications",
    )
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.IN_APP)
    category = models.CharField(max_length=20, choices=Category.choices)
    severity = models.CharField(max_length=20, choices=Severity.choices)
    event_type = models.CharField(max_length=60)
    title = models.CharField(max_length=200)
    summary = models.CharField(max_length=500)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    source_type = models.CharField(max_length=40, choices=SourceType.choices)
    source_reference = models.CharField(max_length=120, blank=True)
    destination_route = models.CharField(max_length=40, choices=DestinationRoute.choices, blank=True)
    destination_object_id = models.CharField(max_length=40, blank=True)
    deduplication_key = models.CharField(max_length=220)
    scheduled_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error_category = models.CharField(max_length=40, blank=True)
    last_error_summary = models.CharField(max_length=500, blank=True)
    processing_token = models.CharField(max_length=120, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=120, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["deduplication_key"],
                name="unique_operational_notification_dedupe_key",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "recipient_membership", "read_at"]),
            models.Index(fields=["tenant", "recipient_membership", "status"]),
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["channel", "status"]),
            models.Index(fields=["next_attempt_at"]),
            models.Index(fields=["tenant", "status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / {self.event_type} / {self.status}"


class TenantOperationalNotificationWorkerRun(models.Model):
    """Execução one-shot do worker de notificações operacionais."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    worker_identifier = models.CharField(max_length=120, blank=True)
    dry_run = models.BooleanField(default=False)
    channel_filter = models.CharField(max_length=20, blank=True)
    tenant_slug = models.CharField(max_length=120, blank=True)
    claimed = models.PositiveIntegerField(default=0)
    delivered = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    cancelled = models.PositiveIntegerField(default=0)
    retry_scheduled = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_summary = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["status", "started_at"]),
        ]

    def __str__(self):
        return f"notif-worker/{self.pk}/{self.status}"
