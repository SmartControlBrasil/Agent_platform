from __future__ import annotations

from typing import Any, Callable

from django.core.exceptions import ValidationError

from agents.infrastructure.livia_configuration import normalize_livia_configuration

ConfigurationNormalizer = Callable[[dict[str, Any]], dict[str, Any]]

_NORMALIZERS: dict[str, ConfigurationNormalizer] = {
    "livia": normalize_livia_configuration,
}


def normalize_agent_configuration(*, agent_slug: str, configuration: dict[str, Any] | None) -> dict[str, Any]:
    if configuration is None:
        configuration = {}
    if not isinstance(configuration, dict):
        raise ValidationError("Configuration must be an object.")
    normalizer = _NORMALIZERS.get(str(agent_slug or "").strip())
    if normalizer is None:
        return configuration
    return normalizer(configuration)


def validate_agent_configuration(*, agent_slug: str, configuration: dict[str, Any] | None) -> None:
    normalize_agent_configuration(agent_slug=agent_slug, configuration=configuration)
