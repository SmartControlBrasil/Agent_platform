import uuid

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from prospecting.domain.dedupe import build_search_result_dedupe_key
from prospecting.domain.enrichment import build_source_key, contains_forbidden_secret, normalize_enrichment_value, normalize_website


TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
ALLOWED_TRANSITIONS = {
    "PENDING": {"DISPATCHED", "FAILED", "CANCELLED"},
    "DISPATCHED": {"RUNNING", "COMPLETED", "FAILED", "CANCELLED"},
    "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
    "COMPLETED": set(),
    "FAILED": {"DISPATCHED", "RUNNING", "COMPLETED"},
    "CANCELLED": {"DISPATCHED", "RUNNING", "COMPLETED"},
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
        if status == self.Status.DISPATCHED:
            if not self.dispatched_at:
                self.dispatched_at = now
            self.error_code = ""
            self.error_message = ""
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
    class ReviewStatus(models.TextChoices):
        UNREVIEWED = "UNREVIEWED", "Não revisado"
        IGNORED = "IGNORED", "Ignorado"

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
    review_status = models.CharField(max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.UNREVIEWED)
    dedupe_key = models.CharField(max_length=320)
    raw_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["search_run", "dedupe_key"], name="unique_search_result_per_run_dedupe")]
        indexes = [
            models.Index(fields=["tenant", "search_run"]),
            models.Index(fields=["search_run", "dedupe_key"]),
            models.Index(fields=["search_run", "review_status"]),
        ]

    def clean(self):
        if self.search_run_id and self.tenant_id and self.search_run.tenant_id != self.tenant_id:
            raise ValidationError({"search_run": "SearchRun must belong to the same tenant."})
        self.name = " ".join((self.name or "").split())
        if not self.name:
            raise ValidationError({"name": "SearchResult name is required."})
        if self.review_status not in self.ReviewStatus.values:
            raise ValidationError({"review_status": "SearchResult review_status is invalid."})
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


class SearchRunExecutionAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="search_run_attempts")
    search_run = models.ForeignKey("prospecting.SearchRun", on_delete=models.CASCADE, related_name="execution_attempts")
    tool_execution = models.OneToOneField("tools.ToolExecution", on_delete=models.PROTECT, related_name="search_run_attempt")
    attempt_number = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["attempt_number"]
        constraints = [
            models.UniqueConstraint(fields=["search_run", "attempt_number"], name="unique_search_run_attempt_number"),
        ]
        indexes = [
            models.Index(fields=["tenant", "search_run"]),
            models.Index(fields=["search_run", "attempt_number"]),
        ]

    def clean(self):
        if self.search_run_id and self.tenant_id and self.search_run.tenant_id != self.tenant_id:
            raise ValidationError({"search_run": "SearchRun must belong to the same tenant."})
        if self.tool_execution_id and self.tenant_id and self.tool_execution.tenant_id != self.tenant_id:
            raise ValidationError({"tool_execution": "ToolExecution must belong to the same tenant."})
        if self.search_run_id and self.tool_execution_id:
            if self.search_run.project_id != self.tool_execution.project_id:
                raise ValidationError({"tool_execution": "ToolExecution project must match SearchRun project."})
            if self.search_run.agent_installation_id != self.tool_execution.agent_installation_id:
                raise ValidationError({"tool_execution": "ToolExecution installation must match SearchRun installation."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.search_run_id} / tentativa {self.attempt_number}"


class Prospect(models.Model):
    class QualificationStatus(models.TextChoices):
        UNQUALIFIED = "UNQUALIFIED", "Não qualificado"
        QUALIFIED = "QUALIFIED", "Qualificado"
        NOT_A_FIT = "NOT_A_FIT", "Fora do perfil"
        ON_HOLD = "ON_HOLD", "Em espera"

    class Priority(models.TextChoices):
        UNSET = "UNSET", "—"
        LOW = "LOW", "Baixa"
        MEDIUM = "MEDIUM", "Média"
        HIGH = "HIGH", "Alta"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="prospects")
    display_name = models.CharField(max_length=220)
    address = models.CharField(max_length=260, blank=True, null=True)
    phone = models.CharField(max_length=80, blank=True, null=True)
    website = models.URLField(max_length=1000, blank=True, null=True)
    maps_url = models.URLField(max_length=1000, blank=True, null=True)
    external_id = models.CharField(max_length=180, blank=True, null=True)
    identity_key = models.CharField(max_length=320)
    qualification_status = models.CharField(
        max_length=16,
        choices=QualificationStatus.choices,
        default=QualificationStatus.UNQUALIFIED,
    )
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.UNSET)
    qualification_note = models.TextField(blank=True, default="")
    qualified_at = models.DateTimeField(null=True, blank=True)
    qualified_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qualified_prospects",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]
        constraints = [models.UniqueConstraint(fields=["tenant", "identity_key"], name="unique_prospect_identity_per_tenant")]
        indexes = [
            models.Index(fields=["tenant", "identity_key"]),
            models.Index(fields=["tenant", "display_name"]),
            models.Index(fields=["tenant", "qualification_status"]),
            models.Index(fields=["tenant", "priority"]),
        ]

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
        if self.qualification_status not in self.QualificationStatus.values:
            raise ValidationError({"qualification_status": "Prospect qualification_status is invalid."})
        if self.priority not in self.Priority.values:
            raise ValidationError({"priority": "Prospect priority is invalid."})
        self.qualification_note = (self.qualification_note or "").strip()
        if len(self.qualification_note) > 2000:
            raise ValidationError({"qualification_note": "Qualification note must be at most 2000 characters."})

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


class ProspectContact(models.Model):
    class SourceType(models.TextChoices):
        MANUAL = "MANUAL", "Manual"
        WEBSITE = "WEBSITE", "Website"
        ENRICHMENT = "ENRICHMENT", "Enrichment"
        OTHER = "OTHER", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="prospect_contacts")
    prospect = models.ForeignKey("prospecting.Prospect", on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=220, blank=True, default="")
    role_title = models.CharField(max_length=160, blank=True, default="")
    email = models.CharField(max_length=320, blank=True, default="")
    phone = models.CharField(max_length=80, blank=True, default="")
    normalized_email = models.CharField(max_length=320, blank=True, default="")
    normalized_phone = models.CharField(max_length=80, blank=True, default="")
    note = models.TextField(blank=True, default="")
    source_type = models.CharField(max_length=16, choices=SourceType.choices, default=SourceType.MANUAL)
    source_reference = models.CharField(max_length=320, blank=True, default="")
    source_url = models.URLField(max_length=1000, blank=True, default="")
    source_enrichment = models.ForeignKey(
        "prospecting.ProspectEnrichment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="derived_contacts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "email", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "prospect", "normalized_email"],
                condition=Q(normalized_email__gt=""),
                name="unique_prospect_contact_email_per_prospect",
            ),
            models.UniqueConstraint(
                fields=["tenant", "prospect", "normalized_phone"],
                condition=Q(normalized_phone__gt=""),
                name="unique_prospect_contact_phone_per_prospect",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "prospect"]),
            models.Index(fields=["tenant", "prospect", "normalized_email"]),
            models.Index(fields=["tenant", "prospect", "normalized_phone"]),
        ]

    def clean(self):
        if self.prospect_id and self.tenant_id and self.prospect.tenant_id != self.tenant_id:
            raise ValidationError({"prospect": "Prospect must belong to the same tenant."})
        if self.source_enrichment_id:
            if self.source_enrichment.tenant_id != self.tenant_id:
                raise ValidationError({"source_enrichment": "Enrichment source must belong to the same tenant."})
            if self.source_enrichment.prospect_id != self.prospect_id:
                raise ValidationError({"source_enrichment": "Enrichment must belong to the same prospect."})
        self.name = " ".join((self.name or "").split())
        self.role_title = " ".join((self.role_title or "").split())
        self.phone = " ".join((self.phone or "").split())
        if self.email:
            self.email = normalize_enrichment_value(ProspectEnrichment.Field.EMAIL, self.email)
        self.note = (self.note or "").strip()
        if len(self.name) > 220:
            raise ValidationError({"name": "Name is too long."})
        if len(self.role_title) > 160:
            raise ValidationError({"role_title": "Role title is too long."})
        if len(self.note) > 2000:
            raise ValidationError({"note": "Note must be at most 2000 characters."})
        if not self.name and not self.email and not self.phone:
            raise ValidationError("Contact requires at least name, email, or phone.")
        if self.email:
            EmailValidator()(self.email)
        if any(contains_forbidden_secret(value) for value in [self.email, self.phone, self.note, self.source_reference]):
            raise ValidationError("Contact cannot store credentials, tokens, cookies, or Authorization data.")
        self.normalized_email = normalize_enrichment_value(ProspectEnrichment.Field.EMAIL, self.email) if self.email else ""
        self.normalized_phone = normalize_enrichment_value(ProspectEnrichment.Field.PHONE, self.phone) if self.phone else ""
        self.source_type = (self.source_type or "").upper()
        if self.source_type not in self.SourceType.values:
            raise ValidationError({"source_type": "Unsupported contact source type."})
        if self.source_url:
            self.source_url = normalize_website(self.source_url)
        self.source_reference = " ".join((self.source_reference or "").split())

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        label = self.name or self.email or self.phone
        return f"{self.prospect.display_name}: {label}"


class ProspectActivity(models.Model):
    class ActivityType(models.TextChoices):
        NOTE = "NOTE", "Nota"
        CALL = "CALL", "Ligação"
        MEETING = "MEETING", "Reunião"
        CONTACT_ATTEMPT = "CONTACT_ATTEMPT", "Tentativa de contato"
        EMAIL_SENT = "EMAIL_SENT", "E-mail enviado"
        REPLY_RECEIVED = "REPLY_RECEIVED", "Retorno recebido"
        VISIT = "VISIT", "Visita"
        OTHER = "OTHER", "Outro"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="prospect_activities")
    prospect = models.ForeignKey("prospecting.Prospect", on_delete=models.CASCADE, related_name="activities")
    contact = models.ForeignKey(
        "prospecting.ProspectContact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activities",
    )
    activity_type = models.CharField(max_length=24, choices=ActivityType.choices, default=ActivityType.NOTE)
    note = models.TextField()
    occurred_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prospect_activities_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-occurred_at", "-created_at"]
        indexes = [
            models.Index(fields=["tenant", "prospect", "occurred_at"]),
            models.Index(fields=["tenant", "prospect", "activity_type"]),
        ]

    def clean(self):
        if self.prospect_id and self.tenant_id and self.prospect.tenant_id != self.tenant_id:
            raise ValidationError({"prospect": "Prospect must belong to the same tenant."})
        if self.contact_id:
            if self.contact.tenant_id != self.tenant_id:
                raise ValidationError({"contact": "Contact must belong to the same tenant."})
            if self.contact.prospect_id != self.prospect_id:
                raise ValidationError({"contact": "Contact must belong to the same prospect."})
        self.activity_type = (self.activity_type or "").upper()
        if self.activity_type not in self.ActivityType.values:
            raise ValidationError({"activity_type": "Invalid activity type."})
        self.note = (self.note or "").strip()
        if not self.note:
            raise ValidationError({"note": "Activity note is required."})
        if len(self.note) > 4000:
            raise ValidationError({"note": "Activity note must be at most 4000 characters."})
        if contains_forbidden_secret(self.note):
            raise ValidationError("Activity cannot store credentials, tokens, cookies, or Authorization data.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.prospect.display_name}: {self.get_activity_type_display()} @ {self.occurred_at:%Y-%m-%d %H:%M}"


class ProspectOutreachDraft(models.Model):
    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        PHONE = "PHONE", "Telefone"
        WHATSAPP = "WHATSAPP", "WhatsApp"
        OTHER = "OTHER", "Outro"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Rascunho"
        READY = "READY", "Pronto"
        ARCHIVED = "ARCHIVED", "Arquivado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="prospect_outreach_drafts")
    prospect = models.ForeignKey("prospecting.Prospect", on_delete=models.CASCADE, related_name="outreach_drafts")
    contact = models.ForeignKey(
        "prospecting.ProspectContact",
        on_delete=models.PROTECT,
        related_name="outreach_drafts",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    subject = models.CharField(max_length=220, blank=True, default="")
    body = models.TextField()
    destination_email = models.CharField(max_length=320, blank=True, default="")
    destination_phone = models.CharField(max_length=80, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    prepared_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prospect_outreach_drafts_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-created_at"]
        indexes = [
            models.Index(fields=["tenant", "prospect", "status"]),
            models.Index(fields=["tenant", "prospect", "channel"]),
        ]

    def clean(self):
        if self.prospect_id and self.tenant_id and self.prospect.tenant_id != self.tenant_id:
            raise ValidationError({"prospect": "Prospect must belong to the same tenant."})
        if self.contact_id:
            if self.contact.tenant_id != self.tenant_id:
                raise ValidationError({"contact": "Contact must belong to the same tenant."})
            if self.contact.prospect_id != self.prospect_id:
                raise ValidationError({"contact": "Contact must belong to the same prospect."})
        self.channel = (self.channel or "").upper()
        if self.channel not in self.Channel.values:
            raise ValidationError({"channel": "Invalid outreach channel."})
        self.status = (self.status or "").upper()
        if self.status not in self.Status.values:
            raise ValidationError({"status": "Invalid outreach draft status."})
        self.subject = " ".join((self.subject or "").split())
        self.body = (self.body or "").strip()
        if not self.body:
            raise ValidationError({"body": "Outreach message body is required."})
        if len(self.body) > 16000:
            raise ValidationError({"body": "Outreach message body must be at most 16000 characters."})
        if len(self.subject) > 220:
            raise ValidationError({"subject": "Subject must be at most 220 characters."})
        if contains_forbidden_secret(self.body) or contains_forbidden_secret(self.subject):
            raise ValidationError("Outreach draft cannot store credentials, tokens, cookies, or Authorization data.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        label = self.subject or self.get_channel_display()
        return f"{self.prospect.display_name}: {label} ({self.get_status_display()})"


class ProspectOutreachSend(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pendente"
        SENDING = "SENDING", "Enviando"
        SENT = "SENT", "Enviado"
        FAILED = "FAILED", "Falha"
        CANCELLED = "CANCELLED", "Cancelado"

    class Provider(models.TextChoices):
        DJANGO_EMAIL = "DJANGO_EMAIL", "Django email"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="prospect_outreach_sends")
    draft = models.OneToOneField(
        "prospecting.ProspectOutreachDraft",
        on_delete=models.PROTECT,
        related_name="outreach_send",
    )
    prospect = models.ForeignKey("prospecting.Prospect", on_delete=models.CASCADE, related_name="outreach_sends")
    contact = models.ForeignKey(
        "prospecting.ProspectContact",
        on_delete=models.PROTECT,
        related_name="outreach_sends",
    )
    channel = models.CharField(max_length=16, choices=ProspectOutreachDraft.Channel.choices)
    destination = models.CharField(max_length=320)
    subject_snapshot = models.CharField(max_length=220, blank=True, default="")
    body_snapshot = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    provider = models.CharField(max_length=32, choices=Provider.choices, default=Provider.DJANGO_EMAIL)
    provider_message_id = models.CharField(max_length=220, blank=True, default="")
    idempotency_key = models.CharField(max_length=220, unique=True)
    requested_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="prospect_outreach_sends_requested",
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=120, blank=True, default="")
    error_message = models.CharField(max_length=500, blank=True, default="")
    prospect_activity = models.OneToOneField(
        "prospecting.ProspectActivity",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_outreach_send",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at", "-created_at"]
        indexes = [
            models.Index(fields=["tenant", "prospect", "status"]),
            models.Index(fields=["tenant", "draft"]),
        ]

    def clean(self):
        if self.prospect_id and self.tenant_id and self.prospect.tenant_id != self.tenant_id:
            raise ValidationError({"prospect": "Prospect must belong to the same tenant."})
        if self.draft_id:
            if self.draft.tenant_id != self.tenant_id:
                raise ValidationError({"draft": "Draft must belong to the same tenant."})
            if self.draft.prospect_id != self.prospect_id:
                raise ValidationError({"draft": "Draft must belong to the same prospect."})
        if self.contact_id:
            if self.contact.tenant_id != self.tenant_id:
                raise ValidationError({"contact": "Contact must belong to the same tenant."})
            if self.contact.prospect_id != self.prospect_id:
                raise ValidationError({"contact": "Contact must belong to the same prospect."})
        self.channel = (self.channel or "").upper()
        if self.channel != ProspectOutreachDraft.Channel.EMAIL:
            raise ValidationError({"channel": "Only EMAIL outreach sends are supported."})
        self.destination = (self.destination or "").strip()
        if not self.destination:
            raise ValidationError({"destination": "Destination email is required."})
        EmailValidator()(self.destination)
        self.body_snapshot = (self.body_snapshot or "").strip()
        if not self.body_snapshot:
            raise ValidationError({"body_snapshot": "Body snapshot is required."})
        self.subject_snapshot = " ".join((self.subject_snapshot or "").split())
        if not self.subject_snapshot:
            raise ValidationError({"subject_snapshot": "Subject snapshot is required for email sends."})
        if contains_forbidden_secret(self.body_snapshot) or contains_forbidden_secret(self.subject_snapshot):
            raise ValidationError("Outreach send cannot store credentials, tokens, cookies, or Authorization data.")
        if contains_forbidden_secret(self.error_message):
            raise ValidationError("Outreach send error message cannot store secrets.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.prospect.display_name}: {self.destination} ({self.get_status_display()})"
