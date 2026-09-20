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
    path("tools/", views.tool_list, name="tool_list"),
    path("tools/<uuid:pk>/", views.tool_detail, name="tool_detail"),
    path("installations/<uuid:installation_id>/tools/add/", views.installation_add_tool, name="installation_add_tool"),
    path("tool-bindings/<uuid:pk>/edit/", views.tool_binding_edit, name="tool_binding_edit"),
    path("tool-bindings/<uuid:pk>/enable/", views.tool_binding_enable, name="tool_binding_enable"),
    path("tool-bindings/<uuid:pk>/disable/", views.tool_binding_disable, name="tool_binding_disable"),
    path("installations/", views.installation_list, name="installation_list"),
    path("installations/<uuid:pk>/", views.installation_detail, name="installation_detail"),
    path("installations/<uuid:pk>/enable/", views.installation_enable, name="installation_enable"),
    path("installations/<uuid:pk>/disable/", views.installation_disable, name="installation_disable"),
]
