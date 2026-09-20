from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError

from agents.models import AgentInstallation

DEFAULT_TARGET_MARKET = ""
DEFAULT_TARGET_REGION = ""
DEFAULT_TARGET_PROFILE = ""
DEFAULT_OBJECTIVE = ""
DEFAULT_MAX_RESULTS = 50

PROSPECTING_CONFIGURATION_LIMITS = {
    "target_market": 160,
    "target_region": 160,
    "target_profile": 200,
    "objective": 500,
}

MAX_RESULTS_MIN = 1
MAX_RESULTS_MAX = 500


@dataclass(frozen=True)
class ProspectingRuntimeConfiguration:
    target_market: str = DEFAULT_TARGET_MARKET
    target_region: str = DEFAULT_TARGET_REGION
    target_profile: str = DEFAULT_TARGET_PROFILE
    objective: str = DEFAULT_OBJECTIVE
    max_results: int = DEFAULT_MAX_RESULTS

    def as_metadata(self) -> dict[str, str | int]:
        return {
            "target_market": self.target_market,
            "target_region": self.target_region,
            "target_profile": self.target_profile,
            "objective": self.objective,
            "max_results": self.max_results,
        }


class ProspectingConfigurationResolver:
    """Resolves and validates Prospecting Agent runtime configuration."""

    def resolve(self, *, installation: AgentInstallation, tenant) -> ProspectingRuntimeConfiguration:
        self._validate_installation_scope(installation=installation, tenant=tenant)
        normalized = normalize_prospecting_configuration(installation.configuration or {})
        return ProspectingRuntimeConfiguration(
            target_market=str(normalized.get("target_market") or DEFAULT_TARGET_MARKET),
            target_region=str(normalized.get("target_region") or DEFAULT_TARGET_REGION),
            target_profile=str(normalized.get("target_profile") or DEFAULT_TARGET_PROFILE),
            objective=str(normalized.get("objective") or DEFAULT_OBJECTIVE),
            max_results=int(normalized.get("max_results") or DEFAULT_MAX_RESULTS),
        )

    def _validate_installation_scope(self, *, installation: AgentInstallation, tenant) -> None:
        if installation.tenant_id != tenant.id:
            raise ValidationError("Prospecting installation tenant does not match execution tenant.")
        if installation.project_id and installation.project.tenant_id != tenant.id:
            raise ValidationError("Prospecting installation project does not belong to execution tenant.")
        if installation.agent_definition.slug != "prospecting":
            raise ValidationError("Prospecting configuration resolver can only handle the Prospecting Agent definition.")
        if installation.agent_version.agent_definition_id != installation.agent_definition_id:
            raise ValidationError("Prospecting installation version does not belong to its definition.")


def normalize_prospecting_configuration(configuration: dict[str, Any]) -> dict[str, str | int]:
    if not isinstance(configuration, dict):
        raise ValidationError("Prospecting configuration must be an object.")

    normalized: dict[str, str | int] = {}
    for key, limit in PROSPECTING_CONFIGURATION_LIMITS.items():
        value = configuration.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise ValidationError({key: "Prospecting configuration values must be strings."})
        text = " ".join(value.strip().split())
        if len(text) > limit:
            raise ValidationError({key: f"Prospecting configuration value must have at most {limit} characters."})
        normalized[key] = text

    raw_max_results = configuration.get("max_results")
    if raw_max_results not in (None, ""):
        try:
            max_results = int(raw_max_results)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"max_results": "Prospecting max_results must be an integer."}) from exc
        if not MAX_RESULTS_MIN <= max_results <= MAX_RESULTS_MAX:
            raise ValidationError(
                {"max_results": f"Prospecting max_results must be between {MAX_RESULTS_MIN} and {MAX_RESULTS_MAX}."}
            )
        normalized["max_results"] = max_results

    return normalized
