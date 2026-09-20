from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from tools.infrastructure.compatibility import is_tool_compatible_with_agent
from tools.infrastructure.prospecting_build_search_plan import normalize_build_search_plan_configuration
from tools.models import AgentToolBinding, ToolDefinition


class AgentToolBindingForm(forms.Form):
    tool_definition = forms.ModelChoiceField(queryset=ToolDefinition.objects.none())
    is_enabled = forms.BooleanField(required=False, initial=True)
    max_queries = forms.IntegerField(min_value=1, max_value=20, required=False, initial=8)

    def __init__(self, *args, installation=None, instance=None, **kwargs):
        self.installation = installation or getattr(instance, "agent_installation", None)
        self.instance = instance
        super().__init__(*args, **kwargs)
        queryset = ToolDefinition.objects.filter(is_active=True).order_by("category", "name")
        if self.installation is not None:
            queryset = [
                tool for tool in queryset
                if is_tool_compatible_with_agent(
                    tool_slug=tool.slug,
                    agent_slug=self.installation.agent_definition.slug,
                )
            ]
        self.fields["tool_definition"].queryset = ToolDefinition.objects.filter(pk__in=[tool.pk for tool in queryset])
        if instance is not None:
            self.fields["tool_definition"].disabled = True
            self.initial.setdefault("tool_definition", instance.tool_definition_id)
            self.initial.setdefault("is_enabled", instance.is_enabled)
            if isinstance(instance.configuration, dict):
                self.initial.setdefault("max_queries", instance.configuration.get("max_queries"))

    def clean(self):
        cleaned = super().clean()
        tool = cleaned.get("tool_definition")
        if self.installation is not None and tool is not None and not is_tool_compatible_with_agent(
            tool_slug=tool.slug, agent_slug=self.installation.agent_definition.slug
        ):
            self.add_error("tool_definition", "Tool is not compatible with this agent installation.")
        if tool is not None and tool.slug == "prospecting.build_search_plan":
            try:
                cleaned["_configuration"] = normalize_build_search_plan_configuration(
                    {"max_queries": cleaned.get("max_queries") or 8}
                )
            except ValidationError as exc:
                self.add_error(None, exc)
        else:
            cleaned["_configuration"] = {}
        return cleaned

    def configuration(self):
        return dict(self.cleaned_data.get("_configuration") or {})
