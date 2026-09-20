from django.urls import path

from .interfaces import api

app_name = "tools_api"

urlpatterns = [
    path("tools/", api.tools_list, name="tools_list"),
    path("agent-installations/<uuid:installation_id>/tools/", api.installation_tools, name="installation_tools"),
    path("tool-bindings/<uuid:binding_id>/execute/", api.execute_binding, name="execute_binding"),
    path("tool-executions/", api.execution_list, name="execution_list"),
    path("tool-executions/<uuid:execution_id>/", api.execution_detail, name="execution_detail"),
    path("tool-executions/<uuid:execution_id>/claim/", api.claim_execution, name="claim_execution"),
    path("tool-executions/<uuid:execution_id>/complete/", api.complete_execution, name="complete_execution"),
    path("tool-executions/<uuid:execution_id>/fail/", api.fail_execution, name="fail_execution"),
]
