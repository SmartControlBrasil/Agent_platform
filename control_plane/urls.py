from django.urls import path

from . import views

app_name = "control_plane"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("tenants/", views.tenant_list, name="tenant_list"),
    path("tenants/<int:pk>/", views.tenant_detail, name="tenant_detail"),
    path("projects/", views.project_list, name="project_list"),
    path("projects/new/", views.project_create, name="project_create"),
    path("projects/<uuid:pk>/", views.project_detail, name="project_detail"),
    path("projects/<uuid:project_id>/install-agent/", views.install_agent, name="install_agent"),
    path("agents/", views.agent_list, name="agent_list"),
    path("agents/<uuid:pk>/", views.agent_detail, name="agent_detail"),
    path("installations/", views.installation_list, name="installation_list"),
    path("installations/<uuid:pk>/", views.installation_detail, name="installation_detail"),
    path("installations/<uuid:pk>/enable/", views.installation_enable, name="installation_enable"),
    path("installations/<uuid:pk>/disable/", views.installation_disable, name="installation_disable"),
]
