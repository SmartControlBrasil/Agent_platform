from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from tenants.models import Tenant


class AgentInstallationError(ValidationError):
    pass


def _ensure_installation_invariants(
    *,
    tenant: Tenant,
    project: Project,
    agent_definition: AgentDefinition,
    agent_version: AgentVersion,
) -> None:
    if project.tenant_id != tenant.id:
        raise AgentInstallationError("Project must belong to the selected tenant.")
    if not tenant.is_active:
        raise AgentInstallationError("Tenant must be active.")
    if not project.is_active:
        raise AgentInstallationError("Project must be active.")
    if not agent_definition.is_active:
        raise AgentInstallationError("Agent definition must be active.")
    if agent_version.status != AgentVersion.Status.ACTIVE:
        raise AgentInstallationError("Agent version must be active.")
    if agent_version.agent_definition_id != agent_definition.id:
        raise AgentInstallationError("Agent version must belong to the selected definition.")


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
    _ensure_installation_invariants(
        tenant=tenant,
        project=project,
        agent_definition=agent_definition,
        agent_version=agent_version,
    )
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


@transaction.atomic
def enable_agent_installation(*, installation: AgentInstallation) -> AgentInstallation:
    installation = AgentInstallation.objects.select_for_update().select_related(
        "tenant", "project", "agent_definition", "agent_version"
    ).get(pk=installation.pk)
    _ensure_installation_invariants(
        tenant=installation.tenant,
        project=installation.project,
        agent_definition=installation.agent_definition,
        agent_version=installation.agent_version,
    )
    installation.is_enabled = True
    installation.full_clean()
    installation.save(update_fields=["is_enabled", "updated_at"])
    return installation


@transaction.atomic
def disable_agent_installation(*, installation: AgentInstallation) -> AgentInstallation:
    installation = AgentInstallation.objects.select_for_update().get(pk=installation.pk)
    installation.is_enabled = False
    installation.full_clean()
    installation.save(update_fields=["is_enabled", "updated_at"])
    return installation


@transaction.atomic
def update_installation_configuration(
    *,
    installation: AgentInstallation,
    configuration: dict,
) -> AgentInstallation:
    installation = AgentInstallation.objects.select_for_update().get(pk=installation.pk)
    installation.configuration = configuration
    installation.full_clean()
    installation.save(update_fields=["configuration", "updated_at"])
    return installation
