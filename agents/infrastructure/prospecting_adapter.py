from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist, ValidationError

from agents.domain.runtime import AgentExecutionContext, AgentRequest, AgentResponse
from agents.infrastructure.prospecting_configuration import ProspectingConfigurationResolver
from agents.models import AgentInstallation
from tenants.models import Tenant


class ProspectingAgentAdapter:
    """Minimal runtime adapter proving a second agent implementation."""

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
        return AgentResponse(
            output=f"Prospecting Agent pronto para {configuration_payload['target_market'] or 'prospeccao planejada'}.",
            status="ready",
            metadata={
                "agent": "prospecting",
                "tenant_id": str(installation.tenant_id),
                "project_id": str(installation.project_id),
                "installation_id": str(installation.id),
                "request_ack": request_input or "prepare prospecting run",
                "configuration": configuration_payload,
            },
        )

    def _load_installation(self, *, context: AgentExecutionContext, tenant: Tenant) -> AgentInstallation:
        return AgentInstallation.objects.select_related("tenant", "project", "agent_definition", "agent_version").get(
            pk=context.installation_id,
            tenant=tenant,
            project_id=context.project_id,
            is_enabled=True,
            agent_definition__slug="prospecting",
        )
