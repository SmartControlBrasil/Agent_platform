from django import forms
from django.core.exceptions import ValidationError

from conversations.models import Conversation, HandoffRequest
from knowledge_base.models import KnowledgeDocument, TenantRagDriveFileManifest
from leads.models import LeadDraft
from tenants.models import AssistantProfile, Tenant
from tenants.origins import normalize_origin

CONVERSATION_LEAD_STATE_CHOICES = [
    (Conversation.LeadState.DISCOVERY, "Descoberta"),
    (Conversation.LeadState.OFFER_HANDOFF, "Oferta de atendimento humano"),
    (Conversation.LeadState.COLLECT_NEED, "Coleta da necessidade"),
    (Conversation.LeadState.COLLECT_NAME_COMPANY, "Nome e empresa"),
    (Conversation.LeadState.COLLECT_CONTACT, "Contato"),
    (Conversation.LeadState.QUALIFIED, "Qualificada"),
    (Conversation.LeadState.CLOSED, "Encerrada"),
]

LEAD_STATUS_CHOICES = [
    (LeadDraft.Status.DRAFT, "Rascunho"),
    (LeadDraft.Status.QUALIFIED, "Qualificado"),
    (LeadDraft.Status.SENT_TO_CRM, "Enviado ao CRM"),
    (LeadDraft.Status.FAILED, "Falha"),
]

HANDOFF_STATUS_CHOICES = [
    (HandoffRequest.Status.PENDING, "Pendente"),
    (HandoffRequest.Status.SENT, "Notificado"),
    (HandoffRequest.Status.RESOLVED, "Resolvido"),
    (HandoffRequest.Status.CANCELLED, "Cancelado"),
]

HANDOFF_PRIORITY_CHOICES = [
    (HandoffRequest.Priority.LOW, "Baixa"),
    (HandoffRequest.Priority.NORMAL, "Normal"),
    (HandoffRequest.Priority.HIGH, "Alta"),
    (HandoffRequest.Priority.URGENT, "Urgente"),
]

HANDOFF_REASON_CHOICES = [
    (HandoffRequest.Reason.EXPLICIT_REQUEST, "Pedido explícito"),
    (HandoffRequest.Reason.QUALIFIED_LEAD, "Lead qualificado"),
    (HandoffRequest.Reason.TECHNICAL_COMPLEXITY, "Complexidade técnica"),
    (HandoffRequest.Reason.SUPPORT_REQUEST, "Suporte"),
    (HandoffRequest.Reason.EMERGENCY_OR_URGENT, "Emergência ou urgência"),
    (HandoffRequest.Reason.MANUAL, "Manual"),
]


class PortalFilterForm(forms.Form):
    def __init__(self, *args, **kwargs):
        tenant_queryset = kwargs.pop("tenant_queryset", None)
        super().__init__(*args, **kwargs)
        if tenant_queryset is not None and "tenant" in self.fields:
            self.fields["tenant"].queryset = tenant_queryset
        for name, field in self.fields.items():
            css_class = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs["class"] = css_class
            if isinstance(field, forms.DateField):
                field.widget.input_type = "date"
            if name == "q":
                field.widget.attrs.setdefault("placeholder", "Buscar")



TENANT_STATUS_CHOICES = [
    ("", "Todos"),
    ("active", "Ativos"),
    ("inactive", "Inativos"),
]


class TenantPortalFilterForm(PortalFilterForm):
    status = forms.ChoiceField(required=False, choices=TENANT_STATUS_CHOICES)
    q = forms.CharField(required=False, max_length=160)


class TenantPortalForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = ["name", "slug", "domain", "is_active"]

    def __init__(self, *args, editing=False, **kwargs):
        self.editing = editing
        super().__init__(*args, **kwargs)
        self.fields["name"].label = "Nome"
        self.fields["slug"].label = "Slug"
        self.fields["domain"].label = "Domínio"
        self.fields["is_active"].label = "Tenant ativo"
        self.fields["domain"].required = False
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"
        if editing:
            self.fields["slug"].disabled = True
            self.fields["slug"].help_text = "Slug não é editado pelo portal para preservar integrações e snippets existentes."

    def clean_domain(self):
        value = str(self.cleaned_data.get("domain") or "").strip()
        if not value:
            return ""
        from tenants.services.onboarding import normalize_allowed_origin

        normalized, _warnings = normalize_allowed_origin(value, required=False)
        return normalized


class AssistantProfilePortalForm(forms.ModelForm):
    class Meta:
        model = AssistantProfile
        fields = [
            "name",
            "business_name",
            "business_domain",
            "short_description",
            "primary_goal",
            "tone",
            "initial_message",
            "widget_title",
            "launcher_label",
            "primary_color",
            "position",
            "placeholder_text",
            "show_branding",
            "is_widget_enabled",
            "use_ai",
        ]
        widgets = {
            "initial_message": forms.Textarea(attrs={"rows": 3}),
            "short_description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        labels = {
            "name": "Nome da assistente",
            "business_name": "Nome do negócio",
            "business_domain": "Domínio de negócio",
            "short_description": "Descrição curta",
            "primary_goal": "Objetivo principal",
            "tone": "Tom",
            "initial_message": "Mensagem inicial",
            "widget_title": "Título do widget",
            "launcher_label": "Texto do launcher",
            "primary_color": "Cor primária",
            "position": "Posição",
            "placeholder_text": "Placeholder",
            "show_branding": "Exibir branding",
            "is_widget_enabled": "Widget habilitado",
            "use_ai": "Usar IA",
            "is_active": "Perfil ativo",
        }
        for name, label in labels.items():
            if name in self.fields:
                self.fields[name].label = label
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"
        self.fields["use_ai"].help_text = "Apenas permite tentativa de IA no perfil; não habilita chaves, gates globais ou side effects."


class AssistantSettingsProfileForm(AssistantProfilePortalForm):
    class Meta(AssistantProfilePortalForm.Meta):
        fields = AssistantProfilePortalForm.Meta.fields + ["is_active"]



class ExecutorPairingClaimForm(forms.Form):
    pairing_code = forms.CharField(
        label="Código de pareamento",
        max_length=32,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "94PU-A366-PR", "autocomplete": "off"}),
    )

    def clean_pairing_code(self):
        value = str(self.cleaned_data.get("pairing_code") or "").strip().upper()
        return "".join(char for char in value if char.isalnum())

class TenantAllowedOriginsPortalForm(forms.Form):
    origins = forms.CharField(
        required=False,
        label="Allowed origins",
        widget=forms.Textarea(attrs={"rows": 6, "class": "form-control", "placeholder": "https://www.exemplo.com.br"}),
        help_text="Informe uma origin por linha. Origins removidas serão desativadas, não excluídas.",
    )

    def __init__(self, *args, tenant=None, **kwargs):
        self.tenant = tenant
        super().__init__(*args, **kwargs)
        if not self.is_bound and tenant is not None:
            active = tenant.allowed_origins.filter(is_active=True).order_by("origin").values_list("origin", flat=True)
            self.fields["origins"].initial = "\n".join(active)

    def clean_origins(self):
        raw = str(self.cleaned_data.get("origins") or "")
        normalized = []
        errors = []
        for line in raw.splitlines():
            value = line.strip()
            if not value:
                continue
            try:
                origin = normalize_origin(value)
            except ValidationError as exc:
                errors.append(f"{value}: {'; '.join(exc.messages)}")
                continue
            if origin not in normalized:
                normalized.append(origin)
        if errors:
            raise forms.ValidationError(errors)
        return normalized


class ConversationFilterForm(PortalFilterForm):
    tenant = forms.ModelChoiceField(queryset=Tenant.objects.order_by("name"), required=False, empty_label="Todos")
    lead_state = forms.ChoiceField(required=False, choices=[("", "Todos")] + CONVERSATION_LEAD_STATE_CHOICES)
    qualified = forms.ChoiceField(required=False, choices=[("", "Todos"), ("yes", "Sim"), ("no", "Não")])
    start_date = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    end_date = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    q = forms.CharField(required=False, max_length=120)


class LeadFilterForm(PortalFilterForm):
    tenant = forms.ModelChoiceField(queryset=Tenant.objects.order_by("name"), required=False, empty_label="Todos")
    status = forms.ChoiceField(required=False, choices=[("", "Todos")] + LEAD_STATUS_CHOICES)
    crm_sent = forms.ChoiceField(required=False, choices=[("", "Todos"), ("yes", "Sim"), ("no", "Não")])
    dispatch_failed = forms.ChoiceField(required=False, choices=[("", "Todos"), ("yes", "Sim"), ("no", "Não")])
    start_date = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    end_date = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    q = forms.CharField(required=False, max_length=160)


class HandoffFilterForm(PortalFilterForm):
    tenant = forms.ModelChoiceField(queryset=Tenant.objects.order_by("name"), required=False, empty_label="Todos")
    status = forms.ChoiceField(required=False, choices=[("", "Todos")] + HANDOFF_STATUS_CHOICES)
    priority = forms.ChoiceField(required=False, choices=[("", "Todas")] + HANDOFF_PRIORITY_CHOICES)
    reason = forms.ChoiceField(required=False, choices=[("", "Todos")] + HANDOFF_REASON_CHOICES)
    start_date = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    end_date = forms.DateField(required=False, input_formats=["%Y-%m-%d"])
    q = forms.CharField(required=False, max_length=160)


class HumanHandoffSettingsForm(forms.ModelForm):
    class Meta:
        model = AssistantProfile
        fields = [
            "human_handoff_enabled",
            "human_handoff_channel",
            "handoff_whatsapp_number",
            "handoff_whatsapp_label",
            "handoff_whatsapp_message",
        ]
        widgets = {
            "handoff_whatsapp_message": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"
        self.fields["human_handoff_enabled"].label = "Ativar handoff humano"
        self.fields["human_handoff_channel"].label = "Canal"
        self.fields["handoff_whatsapp_number"].label = "Número do WhatsApp"
        self.fields["handoff_whatsapp_label"].label = "Texto do botão"
        self.fields["handoff_whatsapp_message"].label = "Mensagem pré-preenchida"
        self.fields["handoff_whatsapp_number"].help_text = "Use telefone internacional. O valor será salvo apenas com dígitos."


KNOWLEDGE_DOCUMENT_STATUS_CHOICES = [
    (KnowledgeDocument.Status.ACTIVE, "Ativo"),
    (KnowledgeDocument.Status.DRAFT, "Rascunho"),
    (KnowledgeDocument.Status.ARCHIVED, "Arquivado"),
]

MANIFEST_STATUS_CHOICES = [
    ("discovered", "Descoberto"),
    ("exported", "Exportado"),
    ("updated", "Atualizado"),
    ("unchanged", "Sem alteração"),
    ("skipped_unsupported", "Ignorado (não suportado)"),
    ("failed", "Falha"),
    ("removed", "Removido"),
    ("unavailable", "Indisponível"),
]

CHUNK_STATUS_CHOICES = [
    ("active", "Ativo"),
    ("replaced", "Substituído"),
    ("failed", "Falha"),
]


class KnowledgeDocumentFilterForm(PortalFilterForm):
    status = forms.ChoiceField(required=False, choices=[("", "Todos")] + KNOWLEDGE_DOCUMENT_STATUS_CHOICES)
    q = forms.CharField(required=False, max_length=160)


class KnowledgeDocumentPortalForm(forms.ModelForm):
    tags_text = forms.CharField(
        required=False,
        label="Tags",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Use vírgula ou uma tag por linha.",
    )

    class Meta:
        model = KnowledgeDocument
        fields = ["tenant", "title", "slug", "status", "source_type", "source_url", "content"]
        widgets = {"content": forms.Textarea(attrs={"rows": 12})}

    def __init__(self, *args, tenant_queryset=None, fixed_tenant=None, **kwargs):
        self.fixed_tenant = fixed_tenant
        super().__init__(*args, **kwargs)
        self.fields["tenant"].label = "Tenant"
        self.fields["title"].label = "Título"
        self.fields["slug"].label = "Slug"
        self.fields["status"].label = "Status"
        self.fields["source_type"].label = "Source type"
        self.fields["source_url"].label = "Source URL"
        self.fields["content"].label = "Conteúdo"
        self.fields["source_type"].required = True
        if tenant_queryset is not None:
            self.fields["tenant"].queryset = tenant_queryset
        if fixed_tenant is not None:
            self.fields["tenant"].initial = fixed_tenant
            self.fields["tenant"].disabled = True
        if self.instance and self.instance.pk and not self.is_bound:
            self.fields["tags_text"].initial = ", ".join(str(tag) for tag in (self.instance.tags or []))
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean_tenant(self):
        if self.fixed_tenant is not None:
            return self.fixed_tenant
        tenant = self.cleaned_data.get("tenant")
        if tenant is None:
            raise forms.ValidationError("Tenant é obrigatório.")
        return tenant

    def clean_tags_text(self):
        raw = str(self.cleaned_data.get("tags_text") or "")
        tags = []
        for value in raw.replace("\r", "\n").replace(",", "\n").splitlines():
            tag = value.strip()
            if tag and tag not in tags:
                tags.append(tag)
        return tags

    def clean_source_url(self):
        return str(self.cleaned_data.get("source_url") or "").strip()

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.tags = self.cleaned_data.get("tags_text") or []
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class KnowledgeImportPortalForm(forms.Form):
    file = forms.FileField(label="Arquivo")
    source_type = forms.CharField(label="Source type", max_length=40, initial="import")
    tags_text = forms.CharField(
        required=False,
        label="Tags",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Use vírgula ou uma tag por linha.",
    )
    status = forms.ChoiceField(label="Status", choices=KNOWLEDGE_DOCUMENT_STATUS_CHOICES, initial=KnowledgeDocument.Status.ACTIVE)
    replace = forms.BooleanField(required=False, label="Atualizar documento existente com mesmo slug")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        from knowledge_base.services.importing import SUPPORTED_EXTENSIONS
        from pathlib import Path

        if Path(uploaded.name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise forms.ValidationError("Formato não suportado. Use .txt, .md ou .markdown.")
        return uploaded

    def clean_tags_text(self):
        raw = str(self.cleaned_data.get("tags_text") or "")
        tags = []
        for value in raw.replace("\r", "\n").replace(",", "\n").splitlines():
            tag = value.strip()
            if tag and tag not in tags:
                tags.append(tag)
        return tags


class KnowledgeTextRetrievalForm(forms.Form):
    query = forms.CharField(
        required=True,
        max_length=500,
        label="Consulta",
        widget=forms.TextInput(attrs={"placeholder": "Digite uma busca textual neste tenant"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["query"].widget.attrs["class"] = "form-control"


class KnowledgeChunkFilterForm(PortalFilterForm):
    status = forms.ChoiceField(required=False, choices=[("", "Todos")] + CHUNK_STATUS_CHOICES)
    is_active = forms.ChoiceField(required=False, choices=[("", "Todos"), ("yes", "Ativo"), ("no", "Inativo")])
    has_embedding = forms.ChoiceField(
        required=False,
        choices=[("", "Todos"), ("yes", "Com embedding"), ("no", "Sem embedding")],
    )
    manifest = forms.ModelChoiceField(queryset=TenantRagDriveFileManifest.objects.none(), required=False, empty_label="Todos")

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tenant is not None:
            self.fields["manifest"].queryset = TenantRagDriveFileManifest.objects.filter(tenant=tenant).order_by("name")


class KnowledgeDiagnosticSearchForm(forms.Form):
    query = forms.CharField(
        required=True,
        max_length=500,
        label="Consulta diagnóstica",
        widget=forms.TextInput(attrs={"placeholder": "Ex.: mármore Carrara para bancada"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["query"].widget.attrs["class"] = "form-control"


class TenantRagConfigurationPortalForm(forms.ModelForm):
    class Meta:
        from knowledge_base.models import TenantRagConfiguration

        model = TenantRagConfiguration
        fields = [
            "retrieval_enabled",
            "min_similarity_score",
            "max_retrieved_chunks",
            "max_context_chars",
            "retrieval_timeout_seconds",
        ]

    def __init__(self, *args, global_limits=None, **kwargs):
        self.global_limits = global_limits
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"
        self.fields["retrieval_enabled"].label = "Habilitar recuperação RAG no chat"
        self.fields["min_similarity_score"].label = "Score mínimo (override)"
        self.fields["min_similarity_score"].required = False
        self.fields["max_retrieved_chunks"].label = "Máximo de chunks (override)"
        self.fields["max_retrieved_chunks"].required = False
        self.fields["max_context_chars"].label = "Orçamento de contexto em caracteres (override)"
        self.fields["max_context_chars"].required = False
        self.fields["retrieval_timeout_seconds"].label = "Timeout da recuperação em segundos (override)"
        self.fields["retrieval_timeout_seconds"].required = False
        limits = global_limits
        if limits is not None:
            self.fields["max_retrieved_chunks"].help_text = (
                f"Deixe vazio para herdar o limite global ({limits.global_max_chunks}). "
                "O tenant só pode restringir esse teto."
            )
            self.fields["max_context_chars"].help_text = (
                f"Deixe vazio para herdar o limite global ({limits.global_max_context_chars} caracteres). "
                "O tenant só pode restringir esse teto."
            )
            self.fields["retrieval_timeout_seconds"].help_text = (
                f"Deixe vazio para herdar o timeout global ({limits.global_timeout_seconds}s). "
                "O tenant só pode reduzir o timeout efetivo."
            )

    def clean(self):
        cleaned = super().clean()
        limits = self.global_limits
        if limits is None:
            return cleaned
        max_chunks = cleaned.get("max_retrieved_chunks")
        if max_chunks is not None and int(max_chunks) > limits.global_max_chunks:
            self.add_error(
                "max_retrieved_chunks",
                f"O valor não pode exceder o limite global de {limits.global_max_chunks}.",
            )
        max_chars = cleaned.get("max_context_chars")
        if max_chars is not None and int(max_chars) > limits.global_max_context_chars:
            self.add_error(
                "max_context_chars",
                f"O valor não pode exceder o limite global de {limits.global_max_context_chars} caracteres.",
            )
        timeout = cleaned.get("retrieval_timeout_seconds")
        if timeout is not None and int(timeout) > limits.global_timeout_seconds:
            self.add_error(
                "retrieval_timeout_seconds",
                f"O valor não pode exceder o timeout global de {limits.global_timeout_seconds}s.",
            )
        return cleaned


PORTAL_RAG_OPERATIONS = [
    ("inventory", "Google Drive: inventariar origem"),
    ("sync_export", "Google Drive: sincronizar textos"),
    ("build_chunks", "Processar documentos e chunks"),
    ("index_embeddings", "Gerar embeddings pendentes"),
    ("full_reindex", "Processar chunks e embeddings"),
]


class KnowledgeBaseOperationRequestForm(forms.Form):
    operation = forms.ChoiceField(choices=PORTAL_RAG_OPERATIONS, label="Operação")
    confirm_reindex = forms.BooleanField(required=False, label="Confirmo o impacto da reindexação completa")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["operation"].widget.attrs["class"] = "form-select"
        self.fields["confirm_reindex"].widget.attrs["class"] = "form-check-input"


class TenantOperationalSettingsForm(forms.ModelForm):
    """Campos operacionais seguros do assistente (sem secrets)."""

    class Meta:
        model = AssistantProfile
        fields = [
            "name",
            "notification_email",
            "human_handoff_enabled",
            "is_widget_enabled",
            "tone",
            "primary_goal",
            "business_name",
            "business_domain",
            "short_description",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"
        self.fields["notification_email"].required = False
        self.fields["notification_email"].help_text = "Destinatário comercial de leads/handoffs. Não dispara e-mail ao salvar."

    def clean_notification_email(self):
        value = str(self.cleaned_data.get("notification_email") or "").strip()
        if not value:
            return ""
        field = forms.EmailField()
        return field.clean(value)


class TenantRagSafeSettingsForm(forms.ModelForm):
    """RAG toggles seguros. folder_id permanece read-only na view."""

    class Meta:
        from knowledge_base.models import TenantRagConfiguration

        model = TenantRagConfiguration
        fields = ["retrieval_enabled", "sync_enabled", "min_similarity_score", "max_retrieved_chunks"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"
        self.fields["min_similarity_score"].required = False
        self.fields["max_retrieved_chunks"].required = False


class TenantOriginAddForm(forms.Form):
    origin = forms.CharField(max_length=255, label="Origin")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["origin"].widget.attrs.update({"class": "form-control", "placeholder": "https://www.exemplo.com.br"})

    def clean_origin(self):
        return normalize_origin(self.cleaned_data.get("origin"))
