import uuid

from django.db import models

from tenants.models import Tenant


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="projects")
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=100)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tenant__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "slug"], name="unique_project_slug_per_tenant"),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["tenant", "slug"]),
        ]

    def __str__(self):
        return f"{self.tenant.slug} / {self.slug}"
