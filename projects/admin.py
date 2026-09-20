from django.contrib import admin

from .models import Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "tenant", "is_active", "created_at")
    list_filter = ("is_active", "tenant")
    search_fields = ("name", "slug", "tenant__name", "tenant__slug")
    readonly_fields = ("id", "created_at", "updated_at")
