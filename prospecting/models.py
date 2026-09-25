import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from prospecting.domain.dedupe import build_search_result_dedupe_key
from prospecting.domain.enrichment import build_source_key, contains_forbidden_secret, normalize_enrichment_value, normalize_website


TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
ALLOWED_TRANSITIONS = {
    "PENDING": {"DISPATCHED", "FAILED", "CANCELLED"},
    "DISPATCHED": {"RUNNING", "COMPLETED", "FAILED", "CANCELLED"},
    "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
}


class SearchRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        DISPATCHED = "DISPATCHED", "Dispatched"
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="search_runs")
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="search_runs")
    agent_installation = models.ForeignKey(
        "agents.AgentInstallation",
        on_delete=models.PROTECT,
        related_name="search_runs",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    target_market = models.CharField(max_length=180, blank=True)
    target_region = models.CharField(max_length=180)
    target_profile = models.CharField(max_length=180, blank=True)
    objective = models.TextField(blank=True)
    queries = models.JSONField(default=list)
    max_results = models.PositiveSmallIntegerField(default=10)
    locale = models.CharField(max_length=16, default="pt-BR")
    agent_platform_execution_id = models.UUIDField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=160, unique=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["tenant", "status"]), models.Index(fields=["project", "status"])]

    def clean(self):
        if self.project_id and self.tenant_id and self.project.tenant_id != self.tenant_id:
            raise ValidationError({"project": "Project must belong to the same tenant."})
        if self.agent_installation_id:
            if self.agent_installation.tenant_id != self.tenant_id:
                raise ValidationError({"agent_installation": "AgentInstallation must belong to the same tenant."})
            if self.agent_installation.project_id != self.project_id:
                raise ValidationError({"agent_installation": "AgentInstallation must belong to the same project."})
            if self.agent_installation.agent_definition.slug != "prospecting":
                raise ValidationError({"agent_installation": "SearchRun agent installation must be the Prospecting Agent."})
        if not isinstance(self.queries, list) or not 1 <= len(self.queries) <= 20:
            raise ValidationError({"queries": "SearchRun requires 1 to 20 queries."})
        normalized_queries = []
        for query in self.queries:
            if not isinstance(query, str) or not " ".join(query.split()):
                raise ValidationError({"queries": "Queries must be non-empty strings."})
            normalized_queries.append(" ".join(query.split()))
        self.queries = normalized_queries
        if not 1 <= self.max_results <= 100:
            raise ValidationError({"max_results": "max_results must be between 1 and 100."})
        self.locale = " ".join((self.locale or "pt-BR").split()) or "pt-BR"
        self.target_market = " ".join((self.target_market or "").split())
        self.target_region = " ".join((self.target_region or "").split())
        self.target_profile = " ".join((self.target_profile or "").split())
        if not self.target_region:
            raise ValidationError({"target_region": "target_region is required."})

    def save(self, *args, **kwargs):
        if not self.idempotency_key:
            if not self.id:
                self.id = uuid.uuid4()
            self.idempotency_key = f"prospecting:search_run:{self.id}"
        self.full_clean()
        super().save(*args, **kwargs)

    def transition_to(self, status, *, error_code="", error_message=""):
        if status == self.status:
            return
        if status not in ALLOWED_TRANSITIONS[self.status]:
            raise ValidationError(f"Invalid SearchRun transition: {self.status} -> {status}.")
        now = timezone.now()
        self.status = status
        if status == self.Status.DISPATCHED and not self.dispatched_at:
            self.dispatched_at = now
        if status == self.Status.COMPLETED:
            self.completed_at = now
            self.error_code = ""
            self.error_message = ""
        if status == self.Status.FAILED:
            self.failed_at = now
            self.error_code = error_code[:80]
            self.error_message = (error_message or "")[:500]

    def __str__(self):
        return f"{self.project.name}: {self.target_region}"


class SearchResult(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="search_results")
    search_run = models.ForeignKey("prospecting.SearchRun", on_delete=models.CASCADE, related_name="results")
    name = models.CharField(max_length=220)
    category = models.CharField(max_length=160, blank=True, null=True)
    address = models.CharField(max_length=260, blank=True, null=True)
    phone = models.CharField(max_length=80, blank=True, null=True)
    website = models.URLField(max_length=1000, blank=True, null=True)
    maps_url = models.URLField(max_length=1000, blank=True, null=True)
    external_id = models.CharField(max_length=180, blank=True, null=True)
    source_query = models.CharField(max_length=260, blank=True, null=True)
    dedupe_key = models.CharField(max_length=320)
    raw_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["search_run", "dedupe_key"], name="unique_search_result_per_run_dedupe")]
        indexes = [models.Index(fields=["tenant", "search_run"]), models.Index(fields=["search_run", "dedupe_key"])]

    def clean(self):
        if self.search_run_id and self.tenant_id and self.search_run.tenant_id != self.tenant_id:
            raise ValidationError({"search_run": "SearchRun must belong to the same tenant."})
        self.name = " ".join((self.name or "").split())
        if not self.name:
            raise ValidationError({"name": "SearchResult name is required."})
        if not self.dedupe_key:
            self.dedupe_key = build_search_result_dedupe_key(
                external_id=self.external_id,
                maps_url=self.maps_url,
                name=self.name,
                address=self.address,
            )

    def save(self, *args, **kwargs):
        if not self.dedupe_key:
            self.dedupe_key = build_search_result_dedupe_key(
                external_id=self.external_id,
                maps_url=self.maps_url,
                name=self.name,
                address=self.address,
            )
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Prospect(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="prospects")
    display_name = models.CharField(max_length=220)
    address = models.CharField(max_length=260, blank=True, null=True)
    phone = models.CharField(max_length=80, blank=True, null=True)
    website = models.URLField(max_length=1000, blank=True, null=True)
    maps_url = models.URLField(max_length=1000, blank=True, null=True)
    external_id = models.CharField(max_length=180, blank=True, null=True)
    identity_key = models.CharField(max_length=320)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "identity_key"], name="unique_prospect_identity_per_tenant")]
        indexes = [models.Index(fields=["tenant", "identity_key"]), models.Index(fields=["tenant", "display_name"])]

    def clean(self):
        self.display_name = " ".join((self.display_name or "").split())
        if not self.display_name:
            raise ValidationError({"display_name": "Prospect display name is required."})
        for field in ["address", "phone", "website", "maps_url", "external_id"]:
            value = getattr(self, field)
            if isinstance(value, str):
                setattr(self, field, " ".join(value.split()) or None)
        self.identity_key = " ".join((self.identity_key or "").split())
        if not self.identity_key:
            raise ValidationError({"identity_key": "Prospect identity key is required."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.display_name


class ProspectSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="prospect_sources")
    prospect = models.ForeignKey("prospecting.Prospect", on_delete=models.CASCADE, related_name="sources")
    search_result = models.ForeignKey("prospecting.SearchResult", on_delete=models.PROTECT, related_name="prospect_sources")
    search_run = models.ForeignKey("prospecting.SearchRun", on_delete=models.PROTECT, related_name="prospect_sources")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["search_result"], name="unique_prospect_source_per_search_result"),
            models.UniqueConstraint(fields=["prospect", "search_result"], name="unique_prospect_source_link"),
        ]
        indexes = [models.Index(fields=["tenant", "prospect"]), models.Index(fields=["tenant", "search_run"])]

    def clean(self):
        if self.prospect_id and self.tenant_id and self.prospect.tenant_id != self.tenant_id:
            raise ValidationError({"prospect": "Prospect must belong to the same tenant."})
        if self.search_result_id and self.tenant_id and self.search_result.tenant_id != self.tenant_id:
            raise ValidationError({"search_result": "SearchResult must belong to the same tenant."})
        if self.search_run_id and self.tenant_id and self.search_run.tenant_id != self.tenant_id:
            raise ValidationError({"search_run": "SearchRun must belong to the same tenant."})
        if self.search_result_id and self.search_run_id and self.search_result.search_run_id != self.search_run_id:
            raise ValidationError({"search_run": "SearchRun must match the SearchResult origin."})

    def save(self, *args, **kwargs):
        if self.search_result_id and not self.search_run_id:
            self.search_run = self.search_result.search_run
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.prospect} <- {self.search_result}"


class ProspectEnrichment(models.Model):
    class Field(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        PHONE = "PHONE", "Phone"
        WEBSITE = "WEBSITE", "Website"
        DOMAIN = "DOMAIN", "Domain"
        ADDRESS = "ADDRESS", "Address"

    class SourceType(models.TextChoices):
        SEARCH_RESULT = "SEARCH_RESULT", "Search result"
        MANUAL = "MANUAL", "Manual"
        WEBSITE = "WEBSITE", "Website"
        OTHER = "OTHER", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="prospect_enrichments")
    prospect = models.ForeignKey("prospecting.Prospect", on_delete=models.CASCADE, related_name="enrichments")
    field = models.CharField(max_length=24, choices=Field.choices)
    value = models.CharField(max_length=1000)
    normalized_value = models.CharField(max_length=1000)
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    source_search_result = models.ForeignKey(
        "prospecting.SearchResult",
        on_delete=models.PROTECT,
        related_name="prospect_enrichments",
        null=True,
        blank=True,
    )
    source_url = models.URLField(max_length=1000, blank=True)
    source_reference = models.CharField(max_length=320, blank=True)
    source_key = models.CharField(max_length=420)
    observed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["field", "value"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "prospect", "field", "normalized_value", "source_key"],
                name="unique_prospect_enrichment_observation",
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "prospect", "field"]),
            models.Index(fields=["tenant", "field", "normalized_value"]),
        ]

    def clean(self):
        if self.prospect_id and self.tenant_id and self.prospect.tenant_id != self.tenant_id:
            raise ValidationError({"prospect": "Prospect must belong to the same tenant."})
        if self.source_search_result_id and self.tenant_id and self.source_search_result.tenant_id != self.tenant_id:
            raise ValidationError({"source_search_result": "SearchResult source must belong to the same tenant."})
        self.field = (self.field or "").upper()
        if self.field not in self.Field.values:
            raise ValidationError({"field": "Unsupported enrichment field."})
        self.source_type = (self.source_type or "").upper()
        if self.source_type not in self.SourceType.values:
            raise ValidationError({"source_type": "Unsupported enrichment source type."})
        self.value = " ".join((self.value or "").split())
        if not self.value:
            raise ValidationError({"value": "Enrichment value is required."})
        if any(contains_forbidden_secret(value) for value in [self.value, self.source_url, self.source_reference]):
            raise ValidationError("Enrichment cannot store credentials, tokens, cookies, or Authorization data.")
        self.normalized_value = normalize_enrichment_value(self.field, self.value)
        if not self.normalized_value:
            raise ValidationError({"value": "Enrichment value cannot be normalized."})
        if self.source_url:
            self.source_url = normalize_website(self.source_url)
        self.source_reference = " ".join((self.source_reference or "").split())
        self.source_key = build_source_key(
            source_type=self.source_type,
            source_search_result=self.source_search_result,
            source_url=self.source_url,
            source_reference=self.source_reference,
        )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.prospect}: {self.field}={self.value}"
