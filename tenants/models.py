import re

from django.core.exceptions import ValidationError
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


WIDGET_POSITION_BOTTOM_RIGHT = "bottom_right"
WIDGET_POSITION_BOTTOM_LEFT = "bottom_left"
WIDGET_POSITION_CHOICES = (
    (WIDGET_POSITION_BOTTOM_RIGHT, "Bottom right"),
    (WIDGET_POSITION_BOTTOM_LEFT, "Bottom left"),
)

DEFAULT_WIDGET_LAUNCHER_LABEL = "Fale com a Lívia"
DEFAULT_WIDGET_PRIMARY_COLOR = "#2563eb"
DEFAULT_WIDGET_PLACEHOLDER_TEXT = "Digite sua mensagem..."
HUMAN_HANDOFF_CHANNEL_DISABLED = "disabled"
HUMAN_HANDOFF_CHANNEL_WHATSAPP = "whatsapp"
HUMAN_HANDOFF_CHANNEL_CHOICES = (
    (HUMAN_HANDOFF_CHANNEL_DISABLED, "Desativado"),
    (HUMAN_HANDOFF_CHANNEL_WHATSAPP, "WhatsApp"),
)
DEFAULT_HANDOFF_WHATSAPP_LABEL = "Falar com um especialista"
DEFAULT_HANDOFF_WHATSAPP_MESSAGE = (
    "Olá, vim pelo atendimento da Lívia e gostaria de continuar com um especialista."
)
MIN_WHATSAPP_NUMBER_LENGTH = 8
MAX_WHATSAPP_NUMBER_LENGTH = 15

validate_widget_color = RegexValidator(
    regex=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$",
    message="Primary color must be a hex color in #RGB or #RRGGBB format.",
)


def normalize_whatsapp_number(value):
    return re.sub(r"\D+", "", str(value or ""))


def validate_whatsapp_number(value):
    digits = normalize_whatsapp_number(value)
    if not digits:
        return
    if not MIN_WHATSAPP_NUMBER_LENGTH <= len(digits) <= MAX_WHATSAPP_NUMBER_LENGTH:
        raise ValidationError(
            "Informe um telefone internacional válido com 8 a 15 dígitos.",
            code="invalid_whatsapp_number",
        )


class Tenant(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80, unique=True)
    domain = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AssistantProfile(models.Model):
    """Legacy compatibility profile for the Lívia runtime and widget.

    New generic Agent Platform configuration belongs on AgentInstallation,
    with AssistantProfile used only as the Lívia fallback while migration is
    progressive.
    """

    tenant = models.OneToOneField(
        Tenant,
        on_delete=models.CASCADE,
        related_name="assistant_profile",
    )
    name = models.CharField(max_length=80, default="Lívia")
    initial_message = models.TextField(
        default="Olá! Sou a Lívia. Como posso te ajudar?"
    )
    tone = models.CharField(max_length=120, default="consultivo, claro e profissional")
    primary_goal = models.CharField(max_length=160, default="qualificar leads")
    business_name = models.CharField(max_length=160, blank=True, default="")
    business_domain = models.CharField(max_length=220, blank=True, default="")
    short_description = models.TextField(blank=True, default="")
    notification_email = models.EmailField(
        blank=True,
        default="",
        help_text="Destinatário comercial de leads/notificações deste tenant. Vazio usa o fallback global.",
    )
    use_ai = models.BooleanField(default=False)
    grounded_synthesis_enabled = models.BooleanField(default=False)
    widget_title = models.CharField(max_length=80, blank=True)
    launcher_label = models.CharField(max_length=80, default=DEFAULT_WIDGET_LAUNCHER_LABEL)
    primary_color = models.CharField(
        max_length=7,
        default=DEFAULT_WIDGET_PRIMARY_COLOR,
        validators=[validate_widget_color],
    )
    position = models.CharField(
        max_length=20,
        choices=WIDGET_POSITION_CHOICES,
        default=WIDGET_POSITION_BOTTOM_RIGHT,
    )
    show_branding = models.BooleanField(default=True)
    collect_contact_hint = models.CharField(max_length=160, blank=True)
    placeholder_text = models.CharField(max_length=120, default=DEFAULT_WIDGET_PLACEHOLDER_TEXT)
    is_widget_enabled = models.BooleanField(default=True)
    human_handoff_enabled = models.BooleanField(default=False)
    human_handoff_channel = models.CharField(
        max_length=20,
        choices=HUMAN_HANDOFF_CHANNEL_CHOICES,
        default=HUMAN_HANDOFF_CHANNEL_DISABLED,
    )
    handoff_whatsapp_number = models.CharField(
        max_length=20,
        blank=True,
        validators=[validate_whatsapp_number],
    )
    handoff_whatsapp_label = models.CharField(
        max_length=80,
        default=DEFAULT_HANDOFF_WHATSAPP_LABEL,
    )
    handoff_whatsapp_message = models.CharField(
        max_length=240,
        default=DEFAULT_HANDOFF_WHATSAPP_MESSAGE,
        blank=True,
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} - {self.tenant.name}"

    def clean(self):
        super().clean()
        self.handoff_whatsapp_number = normalize_whatsapp_number(self.handoff_whatsapp_number)
        validate_whatsapp_number(self.handoff_whatsapp_number)
        if (
            self.human_handoff_enabled
            and self.human_handoff_channel == HUMAN_HANDOFF_CHANNEL_WHATSAPP
            and not self.handoff_whatsapp_number
        ):
            raise ValidationError(
                {"handoff_whatsapp_number": "Informe o número para ativar atendimento por WhatsApp."}
            )

    def save(self, *args, **kwargs):
        self.handoff_whatsapp_number = normalize_whatsapp_number(self.handoff_whatsapp_number)
        super().save(*args, **kwargs)

    @property
    def effective_widget_title(self):
        return self.widget_title.strip() or self.name

    @property
    def effective_business_name(self):
        return self.business_name.strip() or self.tenant.name

    @property
    def has_valid_whatsapp_handoff(self):
        number = normalize_whatsapp_number(self.handoff_whatsapp_number)
        return (
            bool(self.human_handoff_enabled)
            and self.human_handoff_channel == HUMAN_HANDOFF_CHANNEL_WHATSAPP
            and MIN_WHATSAPP_NUMBER_LENGTH <= len(number) <= MAX_WHATSAPP_NUMBER_LENGTH
        )


class TenantMembership(models.Model):
    class Role(models.TextChoices):
        TENANT_ADMIN = "tenant_admin", "Tenant admin"
        MANAGER = "manager", "Manager"
        OPERATOR = "operator", "Operator"
        VIEWER = "viewer", "Viewer"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_memberships",
    )
    role = models.CharField(max_length=30, choices=Role.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tenant_memberships",
    )

    class Meta:
        ordering = ["tenant__name", "user__username"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "user"], name="unique_tenant_membership_per_user"),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["tenant", "role"]),
        ]

    def __str__(self):
        return f"{self.user} / {self.tenant.slug} / {self.role}"


class TenantAllowedOrigin(models.Model):
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="allowed_origins",
    )
    origin = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tenant_allowed_origins",
    )

    class Meta:
        ordering = ["tenant__name", "origin"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "origin"], name="unique_tenant_allowed_origin"),
        ]
        indexes = [
            models.Index(fields=["tenant", "is_active"]),
        ]

    def clean(self):
        super().clean()
        from tenants.origins import normalize_origin

        self.origin = normalize_origin(self.origin)

    def save(self, *args, **kwargs):
        from tenants.origins import normalize_origin

        self.origin = normalize_origin(self.origin)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.tenant.slug} / {self.origin}"
