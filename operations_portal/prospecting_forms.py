from __future__ import annotations

import uuid

from django import forms
from django.utils import timezone

from projects.models import Project
from prospecting.models import (
    Prospect,
    ProspectActivity,
    ProspectContact,
    ProspectContactOutcome,
    ProspectEnrichment,
    ProspectFollowUp,
    ProspectOutreachDraft,
    ProspectOutreachSend,
    SearchResult,
    SearchRun,
)


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
    qualification_status = forms.ChoiceField(
        required=False,
        choices=[("", "Qualificação: todas")] + list(Prospect.QualificationStatus.choices),
    )
    priority = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Prioridade: todas"),
            (Prospect.Priority.HIGH, "Alta"),
            (Prospect.Priority.MEDIUM, "Média"),
            (Prospect.Priority.LOW, "Baixa"),
        ],
    )
    has_contacts = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Contatos: todos"),
            ("yes", "Com contato"),
            ("no", "Sem contato"),
        ],
    )
    discovered_email = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Email encontrado: todos"),
            ("yes", "Com email encontrado"),
            ("no", "Sem email encontrado"),
        ],
    )
    discovered_phone = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Telefone encontrado: todos"),
            ("yes", "Com telefone encontrado"),
            ("no", "Sem telefone encontrado"),
        ],
    )
    has_activities = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Atividades: todas"),
            ("yes", "Com atividade"),
            ("no", "Sem atividade"),
        ],
    )
    follow_up = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Próxima ação: todas"),
            ("with", "Com próxima ação pendente"),
            ("without", "Sem próxima ação pendente"),
            ("overdue", "Com ação atrasada"),
        ],
    )
    outcome = forms.ChoiceField(
        required=False,
        choices=[("", "Resultado: todos")] + list(ProspectContactOutcome.Outcome.choices),
    )
    q = forms.CharField(required=False, max_length=220)

    def __init__(self, *args, project_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = project_queryset or Project.objects.none()
        self.fields["q"].widget.attrs.setdefault("placeholder", "Nome, endereço, website, telefone")


class ProspectContactForm(_StyledForm):
    name = forms.CharField(required=False, max_length=220)
    role_title = forms.CharField(required=False, max_length=160, label="Cargo/Função")
    email = forms.EmailField(required=False, max_length=320)
    phone = forms.CharField(required=False, max_length=80)
    note = forms.CharField(required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2}))

    def clean(self):
        cleaned = super().clean()
        if not (cleaned.get("name") or cleaned.get("email") or cleaned.get("phone")):
            raise forms.ValidationError("Informe ao menos nome, e-mail ou telefone.")
        return cleaned

    def __init__(self, *args, contact=None, **kwargs):
        super().__init__(*args, **kwargs)
        if contact is not None and not self.is_bound:
            self.fields["name"].initial = contact.name
            self.fields["role_title"].initial = contact.role_title
            self.fields["email"].initial = contact.email
            self.fields["phone"].initial = contact.phone
            self.fields["note"].initial = contact.note


class ProspectActivityForm(_StyledForm):
    activity_type = forms.ChoiceField(choices=ProspectActivity.ActivityType.choices, label="Tipo")
    contact = forms.ModelChoiceField(
        queryset=ProspectContact.objects.none(),
        required=False,
        empty_label="Sem contato específico",
        label="Contato",
    )
    occurred_at = forms.DateTimeField(
        label="Data/hora",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"],
    )
    note = forms.CharField(required=True, max_length=4000, widget=forms.Textarea(attrs={"rows": 3}), label="Observação")

    def __init__(self, *args, prospect=None, activity=None, **kwargs):
        super().__init__(*args, **kwargs)
        if prospect is not None:
            self.fields["contact"].queryset = ProspectContact.objects.filter(tenant=prospect.tenant, prospect=prospect).order_by(
                "name", "email"
            )
        if activity is not None and not self.is_bound:
            self.fields["activity_type"].initial = activity.activity_type
            self.fields["contact"].initial = activity.contact_id
            self.fields["occurred_at"].initial = timezone.localtime(activity.occurred_at).strftime("%Y-%m-%dT%H:%M")
            self.fields["note"].initial = activity.note
        elif not self.is_bound:
            self.fields["occurred_at"].initial = timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M")


class ProspectOutreachDraftForm(_StyledForm):
    contact = forms.ModelChoiceField(queryset=ProspectContact.objects.none(), required=True, label="Contato")
    channel = forms.ChoiceField(choices=ProspectOutreachDraft.Channel.choices, label="Canal")
    subject = forms.CharField(required=False, max_length=220, label="Assunto")
    body = forms.CharField(required=True, max_length=16000, widget=forms.Textarea(attrs={"rows": 5}), label="Mensagem")

    def __init__(self, *args, prospect=None, draft=None, **kwargs):
        super().__init__(*args, **kwargs)
        if prospect is not None:
            self.fields["contact"].queryset = ProspectContact.objects.filter(tenant=prospect.tenant, prospect=prospect).order_by(
                "name", "email"
            )
        if draft is not None and not self.is_bound:
            self.fields["contact"].initial = draft.contact_id
            self.fields["channel"].initial = draft.channel
            self.fields["subject"].initial = draft.subject
            self.fields["body"].initial = draft.body

    def clean(self):
        cleaned = super().clean()
        channel = cleaned.get("channel")
        subject = (cleaned.get("subject") or "").strip()
        contact = cleaned.get("contact")
        if channel == ProspectOutreachDraft.Channel.EMAIL and not subject:
            raise forms.ValidationError("Assunto é obrigatório para abordagens por e-mail.")
        if channel == ProspectOutreachDraft.Channel.EMAIL and contact and not contact.email:
            raise forms.ValidationError("O contato selecionado não possui e-mail.")
        if channel in {ProspectOutreachDraft.Channel.PHONE, ProspectOutreachDraft.Channel.WHATSAPP} and contact and not contact.phone:
            raise forms.ValidationError("O contato selecionado não possui telefone.")
        return cleaned


class ProspectContactOutcomeForm(_StyledForm):
    idempotency_key = forms.CharField(widget=forms.HiddenInput())
    contact = forms.ModelChoiceField(queryset=ProspectContact.objects.none(), required=True, label="Contato")
    outreach_send = forms.ModelChoiceField(
        queryset=ProspectOutreachSend.objects.none(),
        required=False,
        empty_label="Sem envio vinculado",
        label="Envio de e-mail",
    )
    outcome = forms.ChoiceField(choices=ProspectContactOutcome.Outcome.choices, label="Resultado")
    occurred_at = forms.DateTimeField(
        label="Data/hora",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"],
    )
    note = forms.CharField(required=False, max_length=4000, widget=forms.Textarea(attrs={"rows": 3}), label="Observação")
    create_follow_up = forms.BooleanField(required=False, initial=False, label="Criar próxima ação")
    follow_up_action_type = forms.ChoiceField(
        required=False,
        choices=[("", "—")] + list(ProspectFollowUp.ActionType.choices),
        label="Tipo da próxima ação",
    )
    follow_up_due_at = forms.DateTimeField(
        required=False,
        label="Data/hora da próxima ação",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"],
    )
    follow_up_note = forms.CharField(required=False, max_length=4000, widget=forms.Textarea(attrs={"rows": 2}), label="Observação da próxima ação")
    follow_up_idempotency_key = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, prospect=None, initial_outreach_send=None, **kwargs):
        super().__init__(*args, **kwargs)
        if prospect is not None:
            self.fields["contact"].queryset = ProspectContact.objects.filter(tenant=prospect.tenant, prospect=prospect).order_by(
                "name", "email"
            )
            self.fields["outreach_send"].queryset = ProspectOutreachSend.objects.filter(
                tenant=prospect.tenant,
                prospect=prospect,
                status=ProspectOutreachSend.Status.SENT,
            ).order_by("-sent_at")
        if not self.is_bound:
            self.fields["occurred_at"].initial = timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M")
            self.fields["idempotency_key"].initial = str(uuid.uuid4())
            self.fields["follow_up_idempotency_key"].initial = str(uuid.uuid4())
            if initial_outreach_send is not None:
                self.fields["outreach_send"].initial = initial_outreach_send.pk
                if initial_outreach_send.contact_id:
                    self.fields["contact"].initial = initial_outreach_send.contact_id

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("create_follow_up") and not cleaned.get("follow_up_action_type"):
            raise forms.ValidationError("Informe o tipo da próxima ação ou desmarque a criação.")
        return cleaned


class FollowUpQueueFilterForm(_StyledForm):
    situation = forms.ChoiceField(
        required=False,
        choices=[
            ("pending", "Pendentes"),
            ("overdue", "Atrasados"),
            ("today", "Hoje"),
            ("upcoming", "Próximos"),
            ("completed", "Concluídos"),
            ("cancelled", "Cancelados"),
            ("all", "Todos"),
        ],
        initial="pending",
        label="Situação",
    )
    action_type = forms.ChoiceField(
        required=False,
        choices=[("", "Tipo: todos")] + list(ProspectFollowUp.ActionType.choices),
        label="Tipo",
    )
    period = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Período: todos"),
            ("today", "Hoje"),
            ("next_7", "Próximos 7 dias"),
            ("next_30", "Próximos 30 dias"),
        ],
        label="Período",
    )
    priority = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Prioridade: todas"),
            (Prospect.Priority.HIGH, "Alta"),
            (Prospect.Priority.MEDIUM, "Média"),
            (Prospect.Priority.LOW, "Baixa"),
            ("UNSET", "Não definida"),
        ],
        label="Prioridade",
    )
    prospect_q = forms.CharField(required=False, max_length=220, label="Prospect")
    contact_q = forms.CharField(required=False, max_length=220, label="Contato")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["prospect_q"].widget.attrs.setdefault("placeholder", "Nome do prospect")
        self.fields["contact_q"].widget.attrs.setdefault("placeholder", "Nome, e-mail ou telefone")


class ProspectFollowUpForm(_StyledForm):
    idempotency_key = forms.CharField(widget=forms.HiddenInput())
    contact = forms.ModelChoiceField(
        queryset=ProspectContact.objects.none(),
        required=False,
        empty_label="Sem contato específico",
        label="Contato",
    )
    action_type = forms.ChoiceField(choices=ProspectFollowUp.ActionType.choices, label="Próxima ação")
    due_at = forms.DateTimeField(
        required=False,
        label="Data/hora",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"],
    )
    note = forms.CharField(required=False, max_length=4000, widget=forms.Textarea(attrs={"rows": 2}), label="Observação")

    def __init__(self, *args, prospect=None, **kwargs):
        super().__init__(*args, **kwargs)
        if prospect is not None:
            self.fields["contact"].queryset = ProspectContact.objects.filter(tenant=prospect.tenant, prospect=prospect).order_by(
                "name", "email"
            )
        if not self.is_bound:
            self.fields["idempotency_key"].initial = str(uuid.uuid4())


class ProspectQualificationForm(_StyledForm):
    qualification_status = forms.ChoiceField(choices=Prospect.QualificationStatus.choices)
    priority = forms.ChoiceField(choices=Prospect.Priority.choices)
    qualification_note = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def __init__(self, *args, prospect=None, **kwargs):
        super().__init__(*args, **kwargs)
        if prospect is not None and not self.is_bound:
            self.fields["qualification_status"].initial = prospect.qualification_status
            self.fields["priority"].initial = prospect.priority
            self.fields["qualification_note"].initial = prospect.qualification_note
        self.fields["qualification_note"].widget.attrs.setdefault(
            "placeholder",
            "Ex.: Hospital privado com site ativo e canal comercial público.",
        )


class ProspectEnrichmentCreateForm(_StyledForm):
    field = forms.ChoiceField(choices=ProspectEnrichment.Field.choices)
    value = forms.CharField(max_length=1000)
    source_type = forms.ChoiceField(choices=ProspectEnrichment.SourceType.choices, initial=ProspectEnrichment.SourceType.MANUAL)
    source_reference = forms.CharField(required=False, max_length=320)
    source_url = forms.URLField(required=False, max_length=1000)


class ProspectingSearchPlanForm(_StyledForm):
    project = forms.ModelChoiceField(queryset=Project.objects.none(), required=True, empty_label=None)
    objective = forms.CharField(required=True, max_length=500, widget=forms.Textarea(attrs={"rows": 3}))
    target_region = forms.CharField(required=True, max_length=160)

    def __init__(self, *args, project_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = project_queryset or Project.objects.none()
        self.fields["objective"].widget.attrs.setdefault(
            "placeholder",
            "Ex.: Encontrar hospitais para apresentar soluções robóticas",
        )
        self.fields["target_region"].widget.attrs.setdefault(
            "placeholder",
            "Ex.: Barueri e Alphaville, SP",
        )


class ProspectingSearchQueryReviewForm(_StyledForm):
    draft_id = forms.CharField(required=True, max_length=64, widget=forms.HiddenInput())
    selected_queries = forms.MultipleChoiceField(
        required=True,
        choices=(),
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, query_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["selected_queries"].choices = tuple(query_choices or ())


class SearchResultFilterForm(_StyledForm):
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Todos"),
            ("unreviewed", "Não revisados"),
            ("promoted", "Promovidos"),
            ("ignored", "Ignorados"),
        ],
    )
    phone = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Telefone: todos"),
            ("with", "Com telefone"),
            ("without", "Sem telefone"),
        ],
    )
    website = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Website: todos"),
            ("with", "Com website"),
            ("without", "Sem website"),
        ],
    )
    q = forms.CharField(required=False, max_length=220)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["q"].widget.attrs.setdefault("placeholder", "Nome, endereço, telefone")


class SearchResultBulkActionForm(_StyledForm):
    action = forms.ChoiceField(
        required=True,
        choices=[
            ("promote", "Promover selecionados"),
            ("ignore", "Ignorar selecionados"),
        ],
    )
    selected_result_ids = forms.MultipleChoiceField(required=True, choices=(), widget=forms.MultipleHiddenInput)

    def __init__(self, *args, result_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["selected_result_ids"].choices = tuple(result_choices or ())
