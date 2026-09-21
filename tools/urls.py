from django.urls import path

from .interfaces import api, executor_api

app_name = "tools_api"

urlpatterns = [

    path("executors/pairing/request/", executor_api.pairing_request, name="executor_pairing_request"),
    path("executors/pairing/<uuid:pairing_id>/status/", executor_api.pairing_status, name="executor_pairing_status"),
    path("executors/pairing/<uuid:pairing_id>/consume/", executor_api.pairing_consume, name="executor_pairing_consume"),
    path("executors/me/", executor_api.me, name="executor_me"),
    path("executors/heartbeat/", executor_api.heartbeat, name="executor_heartbeat"),
    path("executors/tool-executions/", executor_api.execution_queue, name="executor_execution_queue"),
    path("executors/tool-executions/<uuid:execution_id>/claim/", executor_api.claim_execution, name="executor_claim_execution"),
    path("executors/tool-executions/<uuid:execution_id>/complete/", executor_api.complete_execution, name="executor_complete_execution"),
    path("executors/tool-executions/<uuid:execution_id>/fail/", executor_api.fail_execution, name="executor_fail_execution"),
    path("tools/", api.tools_list, name="tools_list"),
    path("agent-installations/<uuid:installation_id>/tools/", api.installation_tools, name="installation_tools"),
    path("tool-bindings/<uuid:binding_id>/execute/", api.execute_binding, name="execute_binding"),
    path("tool-executions/", api.execution_list, name="execution_list"),
    path("tool-executions/<uuid:execution_id>/", api.execution_detail, name="execution_detail"),
    path("tool-executions/<uuid:execution_id>/claim/", api.claim_execution, name="claim_execution"),
    path("tool-executions/<uuid:execution_id>/complete/", api.complete_execution, name="complete_execution"),
    path("tool-executions/<uuid:execution_id>/fail/", api.fail_execution, name="fail_execution"),
]
