from django import forms

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from tenants.models import Tenant

SAFE_CONFIGURATION_FIELDS = ("public_name", "tone", "primary_goal", "short_description")


class ProjectForm(forms.ModelForm):
    tenant = forms.ModelChoiceField(queryset=Tenant.objects.none())

    class Meta:
        model = Project
        fields = ["tenant", "name", "slug", "description", "is_active"]

    def __init__(self, *args, tenants=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tenant"].queryset = tenants or Tenant.objects.none()


class InstallAgentForm(forms.Form):
    agent_definition = forms.ModelChoiceField(queryset=AgentDefinition.objects.none())
    agent_version = forms.ModelChoiceField(queryset=AgentVersion.objects.none())
    name = forms.CharField(max_length=120, required=False)
    public_name = forms.CharField(max_length=120, required=False)
    tone = forms.CharField(max_length=160, required=False)
    primary_goal = forms.CharField(max_length=180, required=False)
    short_description = forms.CharField(widget=forms.Textarea, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agent_definition"].queryset = AgentDefinition.objects.filter(is_active=True).order_by("name")
        self.fields["agent_version"].queryset = AgentVersion.objects.filter(
            status=AgentVersion.Status.ACTIVE,
            agent_definition__is_active=True,
        ).select_related("agent_definition")

    def clean(self):
        cleaned = super().clean()
        definition = cleaned.get("agent_definition")
        version = cleaned.get("agent_version")
        if definition and version and version.agent_definition_id != definition.id:
            self.add_error("agent_version", "Selecione uma versão do agente escolhido.")
        return cleaned

    def configuration(self):
        return {
            field: self.cleaned_data.get(field, "")
            for field in SAFE_CONFIGURATION_FIELDS
            if self.cleaned_data.get(field, "") not in ("", None)
        }


class InstallationConfigurationForm(forms.ModelForm):
    public_name = forms.CharField(max_length=120, required=False)
    tone = forms.CharField(max_length=160, required=False)
    primary_goal = forms.CharField(max_length=180, required=False)
    short_description = forms.CharField(widget=forms.Textarea, required=False)

    class Meta:
        model = AgentInstallation
        fields = []

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance")
        initial = kwargs.setdefault("initial", {})
        if instance is not None and isinstance(instance.configuration, dict):
            for field in SAFE_CONFIGURATION_FIELDS:
                initial.setdefault(field, instance.configuration.get(field, ""))
        super().__init__(*args, **kwargs)

    def configuration(self):
        return {
            field: self.cleaned_data.get(field, "")
            for field in SAFE_CONFIGURATION_FIELDS
            if self.cleaned_data.get(field, "") not in ("", None)
        }
