from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from django.core.exceptions import ValidationError

from agents.models import AgentInstallation

DEFAULT_PUBLIC_NAME = "Lívia"
DEFAULT_TONE = "consultivo, claro e profissional"
DEFAULT_PRIMARY_GOAL = "qualificar leads"
DEFAULT_SHORT_DESCRIPTION = ""

LIVIA_CONFIGURATION_FIELDS = {
    "public_name": 120,
    "tone": 160,
    "primary_goal": 180,
    "short_description": 1000,
}


@dataclass(frozen=True)
class LiviaRuntimeConfiguration:
    public_name: str = DEFAULT_PUBLIC_NAME
    tone: str = DEFAULT_TONE
    primary_goal: str = DEFAULT_PRIMARY_GOAL
    short_description: str = DEFAULT_SHORT_DESCRIPTION

    def as_metadata(self) -> dict[str, str]:
        return {
            "public_name": self.public_name,
            "tone": self.tone,
            "primary_goal": self.primary_goal,
            "short_description": self.short_description,
        }


class LiviaConfigurationResolver:
    """Resolves modern Lívia installation config with AssistantProfile legacy fallback.

    Precedence:
    1. AgentInstallation.configuration for known Lívia fields.
    2. The tenant AssistantProfile, when present and active.
    3. Safe defaults.
    """

    def resolve(self, *, installation: AgentInstallation, tenant, assistant_profile=None) -> LiviaRuntimeConfiguration:
        self._validate_installation_scope(installation=installation, tenant=tenant)
        assistant_profile = self._normalize_assistant_profile(assistant_profile=assistant_profile, tenant=tenant)
        normalized = normalize_livia_configuration(installation.configuration or {})
        return LiviaRuntimeConfiguration(
            public_name=self._pick(
                normalized,
                "public_name",
                getattr(assistant_profile, "name", ""),
                DEFAULT_PUBLIC_NAME,
            ),
            tone=self._pick(
                normalized,
                "tone",
                getattr(assistant_profile, "tone", ""),
                DEFAULT_TONE,
            ),
            primary_goal=self._pick(
                normalized,
                "primary_goal",
                getattr(assistant_profile, "primary_goal", ""),
                DEFAULT_PRIMARY_GOAL,
            ),
            short_description=self._pick(
                normalized,
                "short_description",
                getattr(assistant_profile, "short_description", ""),
                DEFAULT_SHORT_DESCRIPTION,
            ),
        )

    def build_effective_profile(self, *, installation: AgentInstallation, tenant, assistant_profile=None):
        config = self.resolve(installation=installation, tenant=tenant, assistant_profile=assistant_profile)
        return SimpleNamespace(
            name=config.public_name,
            initial_message=str(
                getattr(assistant_profile, "initial_message", "") or "Olá! Sou a Lívia. Como posso te ajudar?"
            ).strip(),
            tone=config.tone,
            primary_goal=config.primary_goal,
            business_name=str(getattr(assistant_profile, "business_name", "") or getattr(tenant, "name", "") or "").strip(),
            business_domain=str(getattr(assistant_profile, "business_domain", "") or "").strip(),
            short_description=config.short_description,
            use_ai=bool(getattr(assistant_profile, "use_ai", False)),
            grounded_synthesis_enabled=bool(getattr(assistant_profile, "grounded_synthesis_enabled", False)),
            human_handoff_enabled=bool(getattr(assistant_profile, "human_handoff_enabled", False)),
            human_handoff_channel=str(getattr(assistant_profile, "human_handoff_channel", "disabled") or "disabled"),
            handoff_whatsapp_number=str(getattr(assistant_profile, "handoff_whatsapp_number", "") or "").strip(),
            handoff_whatsapp_label=str(getattr(assistant_profile, "handoff_whatsapp_label", "Falar com um especialista") or "").strip(),
            handoff_whatsapp_message=str(getattr(assistant_profile, "handoff_whatsapp_message", "") or "").strip(),
            runtime_configuration=config,
            source_assistant_profile=assistant_profile,
        )

    def _validate_installation_scope(self, *, installation: AgentInstallation, tenant) -> None:
        if installation.tenant_id != tenant.id:
            raise ValidationError("Lívia installation tenant does not match execution tenant.")
        if installation.project_id and installation.project.tenant_id != tenant.id:
            raise ValidationError("Lívia installation project does not belong to execution tenant.")
        if installation.agent_definition.slug != "livia":
            raise ValidationError("Lívia configuration resolver can only handle the Lívia agent definition.")
        if installation.agent_version.agent_definition_id != installation.agent_definition_id:
            raise ValidationError("Lívia installation version does not belong to its definition.")

    def _normalize_assistant_profile(self, *, assistant_profile, tenant):
        if assistant_profile is None:
            return None
        if assistant_profile.tenant_id != tenant.id:
            raise ValidationError("Lívia AssistantProfile tenant does not match execution tenant.")
        if not getattr(assistant_profile, "is_active", True):
            return None
        return assistant_profile

    def _pick(self, configuration: dict[str, str], key: str, legacy_value: Any, default: str) -> str:
        configured = configuration.get(key)
        if configured:
            return configured
        legacy = str(legacy_value or "").strip()
        return legacy or default


def normalize_livia_configuration(configuration: dict[str, Any]) -> dict[str, str]:
    if not isinstance(configuration, dict):
        raise ValidationError("Lívia configuration must be an object.")
    normalized: dict[str, str] = {}
    for key, limit in LIVIA_CONFIGURATION_FIELDS.items():
        value = configuration.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise ValidationError({key: "Lívia configuration values must be strings."})
        text = " ".join(value.strip().split())
        if len(text) > limit:
            raise ValidationError({key: f"Lívia configuration value must have at most {limit} characters."})
        normalized[key] = text
    return normalized
