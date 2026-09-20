from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist, ValidationError

from agents.domain.runtime import AgentExecutionContext, AgentRequest, AgentResponse
from agents.infrastructure.prospecting_configuration import ProspectingConfigurationResolver
from agents.models import AgentInstallation
from tenants.models import Tenant
from tools.application.execution import ToolExecutionError, execute_tool
from tools.application.registry import UnknownToolRuntime


class ProspectingAgentAdapter:
    """Runtime adapter for prospecting workflows backed by explicit tool bindings."""

    BUILD_SEARCH_PLAN_TOOL = "prospecting.build_search_plan"

    def __init__(self, configuration_resolver: ProspectingConfigurationResolver | None = None):
        self.configuration_resolver = configuration_resolver or ProspectingConfigurationResolver()

    def execute(self, context: AgentExecutionContext, request: AgentRequest) -> AgentResponse:
        try:
            tenant = Tenant.objects.get(pk=context.tenant_id, is_active=True)
        except (ObjectDoesNotExist, ValueError):
            return AgentResponse(output="", status="tenant_unavailable", metadata={"error": "tenant_unavailable"})

        try:
            installation = self._load_installation(context=context, tenant=tenant)
            runtime_configuration = self.configuration_resolver.resolve(installation=installation, tenant=tenant)
        except (ObjectDoesNotExist, ValueError):
            return AgentResponse(output="", status="installation_unavailable", metadata={"error": "installation_unavailable"})
        except ValidationError as exc:
            return AgentResponse(output="", status="invalid_configuration", metadata={"error": "invalid_configuration", "details": exc.messages})

        request_input = str(request.input or "").strip()
        configuration_payload = runtime_configuration.as_metadata()
        tool_input = {
            "target_market": configuration_payload["target_market"],
            "target_region": configuration_payload["target_region"],
            "target_profile": configuration_payload["target_profile"],
            "objective": configuration_payload["objective"],
        }
        try:
            tool_result = execute_tool(
                installation=installation,
                tool_slug=self.BUILD_SEARCH_PLAN_TOOL,
                input=tool_input,
                user_id=context.user_id,
                metadata={"agent_request": request_input or "prepare prospecting run"},
            )
        except UnknownToolRuntime:
            return self._tool_unavailable_response(
                installation=installation,
                configuration_payload=configuration_payload,
                request_input=request_input,
                error="unknown_tool_runtime",
            )
        except ToolExecutionError as exc:
            return self._tool_unavailable_response(
                installation=installation,
                configuration_payload=configuration_payload,
                request_input=request_input,
                error="tool_unavailable",
                details=exc.messages,
            )
        except ValidationError as exc:
            return self._tool_unavailable_response(
                installation=installation,
                configuration_payload=configuration_payload,
                request_input=request_input,
                error="tool_validation_failed",
                details=exc.messages,
            )

        return AgentResponse(
            output="Prospecting search plan ready.",
            status=tool_result.status,
            metadata={
                "agent": "prospecting",
                "tenant_id": str(installation.tenant_id),
                "project_id": str(installation.project_id),
                "installation_id": str(installation.id),
                "request_ack": request_input or "prepare prospecting run",
                "configuration": configuration_payload,
                "tool": self.BUILD_SEARCH_PLAN_TOOL,
                "tool_result": tool_result.output,
                "tool_metadata": tool_result.metadata,
            },
        )

    def _tool_unavailable_response(
        self,
        *,
        installation: AgentInstallation,
        configuration_payload: dict,
        request_input: str,
        error: str,
        details: list[str] | None = None,
    ) -> AgentResponse:
        metadata = {
            "agent": "prospecting",
            "tenant_id": str(installation.tenant_id),
            "project_id": str(installation.project_id),
            "installation_id": str(installation.id),
            "request_ack": request_input or "prepare prospecting run",
            "configuration": configuration_payload,
            "tool": self.BUILD_SEARCH_PLAN_TOOL,
            "error": error,
        }
        if details:
            metadata["details"] = details
        return AgentResponse(output="", status="tool_unavailable", metadata=metadata)

    def _load_installation(self, *, context: AgentExecutionContext, tenant: Tenant) -> AgentInstallation:
        return AgentInstallation.objects.select_related("tenant", "project", "agent_definition", "agent_version").get(
            pk=context.installation_id,
            tenant=tenant,
            project_id=context.project_id,
            is_enabled=True,
            agent_definition__slug="prospecting",
        )
