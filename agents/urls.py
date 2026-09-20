from django.urls import path

from agents.interfaces import api

app_name = "agents_api"

urlpatterns = [
    path("platform/health/", api.health, name="health"),
    path("agents/", api.agents_list, name="agents_list"),
    path("projects/", api.projects_list, name="projects_list"),
    path("agent-installations/", api.installations_list, name="installations_list"),
    path("agent-installations/<uuid:installation_id>/execute/", api.execute_installation, name="execute_installation"),
]
