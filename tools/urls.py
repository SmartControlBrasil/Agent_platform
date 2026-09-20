from django.urls import path

from .interfaces import api

app_name = "tools_api"

urlpatterns = [
    path("tools/", api.tools_list, name="tools_list"),
    path("agent-installations/<uuid:installation_id>/tools/", api.installation_tools, name="installation_tools"),
    path("tool-bindings/<uuid:binding_id>/execute/", api.execute_binding, name="execute_binding"),
]
