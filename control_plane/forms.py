from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from django import forms

from agents.infrastructure.configuration_validation import normalize_agent_configuration
from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from tenants.models import Tenant


ConfigurationFieldBuilder = Callable[[], dict[str, forms.Field]]


@dataclass(frozen=True)
class AgentConfigurationFormSpec:
    title: str
    description: str
    build_fields: ConfigurationFieldBuilder


def _build_livia_fields() -> dict[str, forms.Field]:
    return {
        "public_name": forms.CharField(max_length=120, required=False, label="Public name"),
        "tone": forms.CharField(max_length=160, required=False, label="Tone"),
        "primary_goal": forms.CharField(max_length=180, required=False, label="Primary goal"),
        "short_description": forms.CharField(widget=forms.Textarea, required=False, label="Short description"),
    }


def _build_prospecting_fields() -> dict[str, forms.Field]:
    return {
        "target_market": forms.CharField(max_length=160, required=False, label="Target market"),
        "target_region": forms.CharField(max_length=160, required=False, label="Target region"),
        "target_profile": forms.CharField(max_length=200, required=False, label="Target profile"),
        "objective": forms.CharField(max_length=500, required=False, widget=forms.Textarea, label="Objective"),
        "max_results": forms.IntegerField(min_value=1, max_value=500, required=False, label="Max results"),
    }


AGENT_CONFIGURATION_FORM_REGISTRY: dict[str, AgentConfigurationFormSpec] = {
    "livia": AgentConfigurationFormSpec(
        title="Configuração da Lívia",
        description="Campos não secretos consumidos pelo runtime da Lívia com fallback legado para AssistantProfile.",
        build_fields=_build_livia_fields,
    ),
    "prospecting": AgentConfigurationFormSpec(
        title="Configuração do Prospecting Agent",
        description="Configuração inicial do runtime de prospecção sem IA, scraping ou integrações externas.",
        build_fields=_build_prospecting_fields,
    ),
}


class AgentConfigurationFormMixin:
    configuration_title = "Configuração"
    configuration_description = "Selecione um agente para carregar os campos específicos de configuração."
    configuration_field_names: tuple[str, ...] = ()
    generic_field_names: tuple[str, ...] = ()

    def _configure_agent_fields(self, *, agent_slug: str, initial_configuration: dict | None = None) -> None:
        spec = AGENT_CONFIGURATION_FORM_REGISTRY.get(agent_slug)
        if spec is None:
            self.configuration_title = "Configuração"
            self.configuration_description = "Selecione um agente para carregar os campos específicos de configuração."
            self.configuration_field_names = ()
            return

        self.configuration_title = spec.title
        self.configuration_description = spec.description
        self.configuration_field_names = tuple(spec.build_fields())
        initial_configuration = initial_configuration or {}
        for name, field in spec.build_fields().items():
            self.fields[name] = field
            if name in initial_configuration:
                self.initial.setdefault(name, initial_configuration.get(name))

    @property
    def has_configuration_fields(self) -> bool:
        return bool(self.configuration_field_names)

    @property
    def visible_generic_fields(self):
        return [self[name] for name in self.generic_field_names if name in self.fields]

    @property
    def visible_configuration_fields(self):
        return [self[name] for name in self.configuration_field_names if name in self.fields]

    def _configuration_from(self, data):
        return {
            field: data.get(field, "")
            for field in self.configuration_field_names
            if data.get(field, "") not in ("", None)
        }

    def _selected_agent_slug(self) -> str:
        value = None
        if getattr(self, "is_bound", False):
            value = self.data.get("agent_definition")
        if not value:
            value = self.initial.get("agent_definition")
        if isinstance(value, AgentDefinition):
            return value.slug
        if not value:
            return ""
        try:
            return AgentDefinition.objects.only("slug").get(pk=value).slug
        except (AgentDefinition.DoesNotExist, TypeError, ValueError):
            return ""


class ProjectForm(forms.ModelForm):
    tenant = forms.ModelChoiceField(queryset=Tenant.objects.none())

    class Meta:
        model = Project
        fields = ["tenant", "name", "slug", "description", "is_active"]

    def __init__(self, *args, tenants=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tenant"].queryset = tenants or Tenant.objects.none()


class InstallAgentForm(AgentConfigurationFormMixin, forms.Form):
    generic_field_names = ("agent_definition", "agent_version", "name")
    agent_definition = forms.ModelChoiceField(queryset=AgentDefinition.objects.none())
    agent_version = forms.ModelChoiceField(queryset=AgentVersion.objects.none())
    name = forms.CharField(max_length=120, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agent_definition"].queryset = AgentDefinition.objects.filter(is_active=True).order_by("name")
        self.fields["agent_version"].queryset = AgentVersion.objects.filter(
            status=AgentVersion.Status.ACTIVE,
            agent_definition__is_active=True,
        ).select_related("agent_definition")
        self._configure_agent_fields(agent_slug=self._selected_agent_slug())

    def clean(self):
        cleaned = super().clean()
        definition = cleaned.get("agent_definition")
        version = cleaned.get("agent_version")
        if definition and version and version.agent_definition_id != definition.id:
            self.add_error("agent_version", "Selecione uma versão do agente escolhido.")
        if definition:
            normalized = normalize_agent_configuration(
                agent_slug=definition.slug,
                configuration=self._configuration_from(cleaned),
            )
            cleaned["_normalized_configuration"] = normalized
        return cleaned

    def configuration(self):
        return dict(self.cleaned_data.get("_normalized_configuration") or self._configuration_from(self.cleaned_data))


class InstallationConfigurationForm(AgentConfigurationFormMixin, forms.ModelForm):
    generic_field_names = ()
    class Meta:
        model = AgentInstallation
        fields = []

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance")
        super().__init__(*args, **kwargs)
        agent_slug = getattr(getattr(instance, "agent_definition", None), "slug", "")
        initial_configuration = instance.configuration if instance is not None and isinstance(instance.configuration, dict) else {}
        self._configure_agent_fields(agent_slug=agent_slug, initial_configuration=initial_configuration)

    def clean(self):
        cleaned = super().clean()
        instance = self.instance
        if instance:
            normalized = normalize_agent_configuration(
                agent_slug=getattr(instance.agent_definition, "slug", ""),
                configuration=self._configuration_from(cleaned),
            )
            cleaned["_normalized_configuration"] = normalized
        return cleaned

    def configuration(self):
        return dict(self.cleaned_data.get("_normalized_configuration") or self._configuration_from(self.cleaned_data))
