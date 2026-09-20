from django.contrib import admin
from django.urls import include, path

from tenants.views import tenant_install_json, tenant_install_page

from .views import healthcheck

urlpatterns = [
    path("health/", healthcheck),
    path("install/<slug:tenant_slug>/", tenant_install_page),
    path("install/<slug:tenant_slug>.json", tenant_install_json),
    path("admin/", admin.site.urls),
    path("painel/", include("operations_portal.urls", namespace="operations_portal")),
    path("platform/", include("control_plane.urls", namespace="control_plane")),
    path("api/v1/", include("agents.urls", namespace="agents_api")),
    path("api/v1/", include("tools.urls", namespace="tools_api")),
    path("api/", include("assistant_core.urls")),
    path("", include("widget.urls")),
]
