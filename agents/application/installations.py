from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from tenants.models import Tenant


@transaction.atomic
def install_agent(
    *,
    tenant: Tenant,
    project: Project,
    agent_definition: AgentDefinition,
    agent_version: AgentVersion,
    name: str = "",
    configuration: dict | None = None,
) -> AgentInstallation:
    installation = AgentInstallation(
        tenant=tenant,
        project=project,
        agent_definition=agent_definition,
        agent_version=agent_version,
        name=name or agent_definition.name,
        configuration=configuration or {},
    )
    installation.full_clean()
    installation.save()
    return installation
