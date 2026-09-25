from __future__ import annotations

from django import forms

from projects.models import Project
from prospecting.models import ProspectEnrichment, SearchRun


class _StyledForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            elif isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"


class SearchRunFilterForm(_StyledForm):
    status = forms.ChoiceField(required=False, choices=[("", "Todos")] + list(SearchRun.Status.choices))
    project = forms.ModelChoiceField(queryset=Project.objects.none(), required=False, empty_label="Todos")
    q = forms.CharField(required=False, max_length=180)

    def __init__(self, *args, project_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = project_queryset or Project.objects.none()
        self.fields["q"].widget.attrs.setdefault("placeholder", "Região, objetivo ou query")


class ProspectFilterForm(_StyledForm):
    project = forms.ModelChoiceField(queryset=Project.objects.none(), required=False, empty_label="Todos")
    q = forms.CharField(required=False, max_length=220)

    def __init__(self, *args, project_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = project_queryset or Project.objects.none()
        self.fields["q"].widget.attrs.setdefault("placeholder", "Nome, endereço, website, telefone")


class ProspectEnrichmentCreateForm(_StyledForm):
    field = forms.ChoiceField(choices=ProspectEnrichment.Field.choices)
    value = forms.CharField(max_length=1000)
    source_type = forms.ChoiceField(choices=ProspectEnrichment.SourceType.choices, initial=ProspectEnrichment.SourceType.MANUAL)
    source_reference = forms.CharField(required=False, max_length=320)
    source_url = forms.URLField(required=False, max_length=1000)
