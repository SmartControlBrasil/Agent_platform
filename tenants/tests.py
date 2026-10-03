from io import StringIO
import uuid
from unittest.mock import patch
from datetime import timedelta

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from audit.models import AuditEvent
from conversations.models import ChatRequest
from knowledge_base.models import KnowledgeDocument
from tenants.models import AssistantProfile, Tenant, TenantAllowedOrigin
from tenants.services.database_validation import build_database_validation_report
from tenants.services.install_package import TenantInstallPackageService
from tenants.services.onboarding import (
    ONBOARDING_CREATED,
    ONBOARDING_UNCHANGED,
    ONBOARDING_UPDATED,
    TenantOnboardingConflict,
    TenantOnboardingService,
    build_widget_snippet,
    normalize_allowed_origin,
)


class SeedInitialTenantsCommandTests(TestCase):
    def test_seed_initial_tenants_is_idempotent(self):
        call_command("seed_initial_tenants", verbosity=0)
        call_command("seed_initial_tenants", verbosity=0)

        self.assertEqual(Tenant.objects.filter(slug="smart-control-brasil").count(), 1)
        self.assertEqual(Tenant.objects.filter(slug="granimarmores-pitondo").count(), 1)
        self.assertEqual(AssistantProfile.objects.count(), 2)
        smart_tenant = Tenant.objects.get(slug="smart-control-brasil")
        self.assertTrue(smart_tenant.is_active)
        self.assertIn("Smart Control Brasil", smart_tenant.assistant_profile.initial_message)



class TenantOnboardingServiceTests(TestCase):
    def setUp(self):
        self.service = TenantOnboardingService()


    def test_onboard_complete_result_includes_origins_readiness_install_package_and_safe_defaults(self):
        result = self.service.onboard(
            slug="complete-client",
            name="Complete Client",
            domain="https://cliente.com.br",
            allowed_origins=["https://cliente.com.br", "https://www.cliente.com.br"],
        )

        self.assertEqual(result.status, ONBOARDING_CREATED)
        self.assertEqual(result.allowed_origins, ["https://cliente.com.br", "https://www.cliente.com.br"])
        self.assertEqual(result.origins_added, ["https://cliente.com.br", "https://www.cliente.com.br"])
        self.assertEqual(result.origins_existing, [])
        self.assertEqual(result.origins_removed, [])
        self.assertFalse(result.assistant_profile.use_ai)
        self.assertFalse(result.assistant_profile.is_widget_enabled)
        self.assertEqual(result.readiness_status, "NOT_READY")
        self.assertIsNotNone(result.install_package)
        self.assertIn('data-tenant="complete-client"', result.install_package.snippet)
        self.assertEqual(TenantAllowedOrigin.objects.filter(tenant=result.tenant, is_active=True).count(), 2)

    def test_onboard_is_idempotent_and_reports_unchanged(self):
        first = self.service.onboard(
            slug="idem-client",
            name="Idem Client",
            domain="https://idem.example",
            widget_enabled=True,
        )
        second = self.service.onboard(
            slug="idem-client",
            name="Idem Client",
            domain="https://idem.example",
            widget_enabled=True,
        )

        self.assertEqual(first.status, ONBOARDING_CREATED)
        self.assertEqual(second.status, ONBOARDING_UNCHANGED)
        self.assertEqual(second.origins_added, [])
        self.assertEqual(second.origins_existing, ["https://idem.example"])
        self.assertEqual(second.origins_removed, [])
        self.assertEqual(Tenant.objects.filter(slug="idem-client").count(), 1)
        self.assertEqual(AssistantProfile.objects.filter(tenant=second.tenant).count(), 1)
        self.assertEqual(TenantAllowedOrigin.objects.filter(tenant=second.tenant, origin="https://idem.example").count(), 1)

    def test_onboard_reports_updated_for_existing_allowed_update(self):
        self.service.onboard(slug="update-client", name="Update Client", domain="https://update.example")
        result = self.service.onboard(
            slug="update-client",
            name="Update Client Novo",
            domain="https://update.example",
        )

        self.assertEqual(result.status, ONBOARDING_UPDATED)
        self.assertEqual(result.tenant.name, "Update Client Novo")

    def test_onboard_conflict_does_not_overwrite_existing_tenant(self):
        Tenant.objects.create(slug="conflict-client", name="Original", domain="https://original.example")

        with self.assertRaises(TenantOnboardingConflict):
            self.service.onboard(
                slug="conflict-client",
                name="Novo",
                domain="https://novo.example",
                allow_update_existing=False,
            )

        tenant = Tenant.objects.get(slug="conflict-client")
        self.assertEqual(tenant.name, "Original")
        self.assertEqual(tenant.domain, "https://original.example")
        self.assertTrue(AuditEvent.objects.filter(action="tenant.onboarding_failed", object_id="conflict-client").exists())

    def test_onboard_rolls_back_partial_state_and_audits_failure(self):
        with patch.object(self.service, "_sync_origins", side_effect=RuntimeError("forced onboarding failure")):
            with self.assertRaises(RuntimeError):
                self.service.onboard(
                    slug="rollback-client",
                    name="Rollback Client",
                    domain="https://rollback.example",
                )

        self.assertFalse(Tenant.objects.filter(slug="rollback-client").exists())
        self.assertFalse(AssistantProfile.objects.filter(tenant__slug="rollback-client").exists())
        self.assertFalse(TenantAllowedOrigin.objects.filter(tenant__slug="rollback-client").exists())
        self.assertTrue(AuditEvent.objects.filter(action="tenant.onboarding_failed", object_id="rollback-client").exists())

    def test_onboard_audits_completion_and_origin_creation_without_secrets(self):
        result = self.service.onboard(
            slug="audit-client",
            name="Audit Client",
            domain="https://audit.example",
            source="tests.onboarding",
        )

        self.assertTrue(AuditEvent.objects.filter(tenant=result.tenant, action="tenant.onboarding_completed").exists())
        self.assertTrue(AuditEvent.objects.filter(tenant=result.tenant, action="tenant_origin.created").exists())
        payload = " ".join(str(event.metadata) + str(event.after_data) for event in AuditEvent.objects.filter(tenant=result.tenant))
        self.assertNotIn("secret", payload.lower())
        self.assertNotIn("token", payload.lower())

    def test_onboard_deactivates_missing_origins_only_when_explicit(self):
        tenant = self.service.onboard(
            slug="origin-sync",
            name="Origin Sync",
            domain="https://origin.example",
            allowed_origins=["https://origin.example", "https://www.origin.example"],
        ).tenant

        result = self.service.onboard(
            slug="origin-sync",
            name="Origin Sync",
            domain="https://origin.example",
            allowed_origins=["https://origin.example"],
            deactivate_missing_origins=True,
        )

        self.assertEqual(result.origins_existing, ["https://origin.example"])
        self.assertEqual(result.origins_removed, ["https://www.origin.example"])
        self.assertTrue(TenantAllowedOrigin.objects.get(tenant=tenant, origin="https://origin.example").is_active)
        self.assertFalse(TenantAllowedOrigin.objects.get(tenant=tenant, origin="https://www.origin.example").is_active)

    def test_onboard_rejects_wildcard_origin(self):
        with self.assertRaises(ValidationError):
            self.service.onboard(
                slug="wildcard-client",
                name="Wildcard Client",
                domain="https://wildcard.example",
                allowed_origins=["*"],
            )
        self.assertFalse(Tenant.objects.filter(slug="wildcard-client").exists())

    def test_onboard_creates_tenant_when_missing(self):
        result = self.service.onboard(
            slug="canecadegaragem",
            name="Caneca de Garagem",
            domain="https://canecadegaragem.com.br",
        )

        self.assertTrue(result.created_tenant)
        self.assertTrue(Tenant.objects.filter(slug="canecadegaragem").exists())
        self.assertEqual(result.tenant.domain, "https://canecadegaragem.com.br")

    def test_onboard_updates_existing_tenant_without_duplicate(self):
        Tenant.objects.create(slug="granimarmores-pitondo", name="Antigo", domain="https://old.example")

        result = self.service.onboard(
            slug="granimarmores-pitondo",
            name="Granimármores Pitondo",
            domain="https://www.granimarmorespitondo.com.br/",
        )

        self.assertFalse(result.created_tenant)
        self.assertEqual(Tenant.objects.filter(slug="granimarmores-pitondo").count(), 1)
        self.assertEqual(result.tenant.name, "Granimármores Pitondo")
        self.assertEqual(result.allowed_origin, "https://www.granimarmorespitondo.com.br")

    def test_onboard_creates_assistant_profile(self):
        result = self.service.onboard(
            slug="profile-new",
            name="Profile New",
            domain="profile-new.com.br",
            assistant_name="Lívia",
            primary_goal="qualificar atendimento comercial",
        )

        self.assertTrue(result.created_profile)
        self.assertEqual(AssistantProfile.objects.count(), 1)
        self.assertEqual(result.assistant_profile.primary_goal, "qualificar atendimento comercial")

    def test_onboard_stores_business_domain_and_short_description(self):
        result = self.service.onboard(
            slug="logistica-demo",
            name="Logística Demo",
            domain="logistica.example",
            business_domain="logística e transporte de cargas",
            short_description="Qualifica fretes com origem, destino, prazo e volume.",
            seed_knowledge=True,
        )

        self.assertEqual(result.assistant_profile.business_domain, "logística e transporte de cargas")
        self.assertEqual(result.assistant_profile.short_description, "Qualifica fretes com origem, destino, prazo e volume.")
        document = KnowledgeDocument.objects.get(tenant=result.tenant, slug="sobre-logistica-demo")
        self.assertIn("logística e transporte", document.content)
        self.assertIn("Qualifica fretes", document.content)

    def test_assistant_profile_notification_email_overrides_global_fallback(self):
        from leads.models import LeadDraft
        from leads.services.lead_notification import LeadNotificationService
        from conversations.models import Conversation
        from django.test import override_settings
        from django.core import mail

        tenant = Tenant.objects.create(slug="notify-tenant", name="Notify Tenant", domain="notify.example")
        profile = AssistantProfile.objects.create(
            tenant=tenant,
            name="Lívia",
            notification_email="comercial@smartcontrolbrasil.com.br",
        )
        conversation = Conversation.objects.create(tenant=tenant, session_id="notify-session")
        lead = LeadDraft.objects.create(
            tenant=tenant,
            conversation=conversation,
            name="Maria",
            phone="11987654321",
            email="maria@example.com",
            company="Maria Servicos",
            need_summary="Preciso de automação industrial Mitsubishi.",
            status=LeadDraft.Status.QUALIFIED,
            qualification_data={"collection_active": True, "commercial_intent": True, "collection_trigger_reason": "test"},
        )
        with override_settings(
            LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
            LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
            LIVIA_LEAD_NOTIFICATION_EMAIL="contato@smartcontrolbrasil.com.br",
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        ):
            result = LeadNotificationService().notify(lead)
        self.assertTrue(result.success)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["comercial@smartcontrolbrasil.com.br"])
        self.assertNotEqual(profile.notification_email, "contato@smartcontrolbrasil.com.br")

    def test_onboard_updates_existing_assistant_profile_without_duplicate(self):
        tenant = Tenant.objects.create(slug="profile-existing", name="Profile Existing", domain="https://old.example")
        AssistantProfile.objects.create(tenant=tenant, name="Assistente antiga", primary_goal="antigo")

        result = self.service.onboard(
            slug="profile-existing",
            name="Profile Existing",
            domain="profile-existing.com.br",
            assistant_name="Lívia Nova",
            primary_goal="novo objetivo",
        )

        self.assertFalse(result.created_profile)
        self.assertEqual(AssistantProfile.objects.count(), 1)
        self.assertEqual(result.assistant_profile.name, "Lívia Nova")
        self.assertEqual(result.assistant_profile.primary_goal, "novo objetivo")

    def test_build_widget_snippet_uses_defaults(self):
        snippet = build_widget_snippet("smart-control-brasil")

        self.assertIn('src="https://livia.smartcontrolbrasil.com.br/widget.js"', snippet)
        self.assertIn('data-tenant="smart-control-brasil"', snippet)
        self.assertIn('data-api-url="https://livia.smartcontrolbrasil.com.br/api/chat/"', snippet)

    def test_normalize_domain_with_https(self):
        allowed_origin, warnings = normalize_allowed_origin("https://example.com.br/")

        self.assertEqual(allowed_origin, "https://example.com.br")
        self.assertNotIn("Domain uses http; production embeds should use https.", warnings)

    def test_normalize_domain_without_scheme_assumes_https(self):
        allowed_origin, warnings = normalize_allowed_origin("example.com.br")

        self.assertEqual(allowed_origin, "https://example.com.br")
        self.assertIn("Domain did not include a scheme; https:// was assumed.", warnings)

    def test_seed_knowledge_creates_base_document(self):
        result = self.service.onboard(
            slug="seed-new",
            name="Seed New",
            domain="seed-new.com.br",
            primary_goal="qualificar oportunidades",
            seed_knowledge=True,
        )

        self.assertEqual(result.created_knowledge_count, 1)
        document = KnowledgeDocument.objects.get(tenant=result.tenant, title="Sobre Seed New")
        self.assertEqual(document.source_type, "manual")
        self.assertTrue(document.is_active)
        self.assertEqual(document.tags, ["institucional", "onboarding"])

    def test_seed_knowledge_is_idempotent(self):
        first = self.service.onboard(
            slug="seed-idempotent",
            name="Seed Idempotent",
            domain="seed-idempotent.com.br",
            seed_knowledge=True,
        )
        second = self.service.onboard(
            slug="seed-idempotent",
            name="Seed Idempotent",
            domain="seed-idempotent.com.br",
            seed_knowledge=True,
        )

        self.assertEqual(first.created_knowledge_count, 1)
        self.assertEqual(second.created_knowledge_count, 0)
        self.assertEqual(KnowledgeDocument.objects.filter(tenant=second.tenant).count(), 1)

    def test_dry_run_does_not_write_to_database(self):
        result = self.service.onboard(
            slug="dry-run-tenant",
            name="Dry Run Tenant",
            domain="dry-run.example.com",
            seed_knowledge=True,
            dry_run=True,
        )

        self.assertTrue(result.created_tenant)
        self.assertEqual(result.created_knowledge_count, 1)
        self.assertFalse(Tenant.objects.filter(slug="dry-run-tenant").exists())
        self.assertEqual(AssistantProfile.objects.count(), 0)
        self.assertEqual(KnowledgeDocument.objects.count(), 0)

    def test_onboard_accepts_widget_customization(self):
        result = self.service.onboard(
            slug="custom-widget",
            name="Custom Widget",
            domain="custom.example",
            widget_title="Lívia Custom",
            launcher_label="Chamar atendimento",
            primary_color="#0f766e",
            position="bottom_left",
            placeholder_text="Como podemos ajudar?",
            widget_enabled=False,
        )

        profile = result.assistant_profile
        self.assertEqual(profile.widget_title, "Lívia Custom")
        self.assertEqual(profile.launcher_label, "Chamar atendimento")
        self.assertEqual(profile.primary_color, "#0f766e")
        self.assertEqual(profile.position, "bottom_left")
        self.assertEqual(profile.placeholder_text, "Como podemos ajudar?")
        self.assertFalse(profile.is_widget_enabled)

    def test_onboard_rejects_invalid_primary_color(self):
        with self.assertRaises(ValidationError):
            self.service.onboard(
                slug="bad-color",
                name="Bad Color",
                domain="bad-color.example",
                primary_color="blue",
            )

        self.assertFalse(AssistantProfile.objects.filter(tenant__slug="bad-color").exists())

    def test_onboard_rejects_invalid_position(self):
        with self.assertRaises(ValidationError):
            self.service.onboard(
                slug="bad-position",
                name="Bad Position",
                domain="bad-position.example",
                position="top_right",
            )

        self.assertFalse(AssistantProfile.objects.filter(tenant__slug="bad-position").exists())


class OnboardTenantCommandTests(TestCase):

    def test_onboard_tenant_command_accepts_origin_alias_without_domain(self):
        output = StringIO()

        call_command(
            "onboard_tenant",
            "--slug",
            "command-origin",
            "--name",
            "Command Origin",
            "--origin",
            "https://command-origin.example",
            "--apply",
            stdout=output,
        )

        tenant = Tenant.objects.get(slug="command-origin")
        self.assertEqual(tenant.domain, "https://command-origin.example")
        self.assertTrue(TenantAllowedOrigin.objects.filter(tenant=tenant, origin="https://command-origin.example").exists())
        self.assertIn("Status: CREATED", output.getvalue())
        self.assertTrue(AuditEvent.objects.filter(tenant=tenant, action="tenant.onboarding_completed", metadata__source="management.onboard_tenant").exists())


    def test_tenant_onboard_alias_command_runs_dry_run(self):
        output = StringIO()

        call_command(
            "tenant_onboard",
            "--slug",
            "alias-dry-run",
            "--name",
            "Alias Dry Run",
            "--origin",
            "https://alias-dry-run.example",
            "--dry-run",
            stdout=output,
        )

        self.assertFalse(Tenant.objects.filter(slug="alias-dry-run").exists())
        self.assertIn("DRY RUN", output.getvalue())
        self.assertIn("Status: CREATED", output.getvalue())

    def test_onboard_tenant_command_prints_snippet(self):
        output = StringIO()

        call_command(
            "onboard_tenant",
            "--slug",
            "command-tenant",
            "--name",
            "Command Tenant",
            "--domain",
            "command.example.com",
            "--apply",
            stdout=output,
        )

        content = output.getvalue()
        self.assertIn("Snippet do widget:", content)
        self.assertIn('data-tenant="command-tenant"', content)
        self.assertIn("Tenant: command-tenant (criado)", content)

    def test_onboard_tenant_command_accepts_widget_fields(self):
        output = StringIO()

        call_command(
            "onboard_tenant",
            "--slug",
            "command-widget",
            "--name",
            "Command Widget",
            "--domain",
            "command-widget.example",
            "--widget-title",
            "Lívia Command",
            "--launcher-label",
            "Abrir chat",
            "--primary-color",
            "#abc",
            "--position",
            "bottom_left",
            "--placeholder-text",
            "Digite aqui",
            "--disable-widget",
            "--apply",
            stdout=output,
        )

        profile = Tenant.objects.get(slug="command-widget").assistant_profile
        self.assertEqual(profile.widget_title, "Lívia Command")
        self.assertEqual(profile.launcher_label, "Abrir chat")
        self.assertEqual(profile.primary_color, "#abc")
        self.assertEqual(profile.position, "bottom_left")
        self.assertEqual(profile.placeholder_text, "Digite aqui")
        self.assertFalse(profile.is_widget_enabled)
        self.assertIn("Widget: inativo", output.getvalue())

    def test_onboard_tenant_command_rejects_invalid_color(self):
        with self.assertRaises(CommandError):
            call_command(
                "onboard_tenant",
                "--slug",
                "bad-command-color",
                "--name",
                "Bad Command Color",
                "--domain",
                "bad-command-color.example",
                "--primary-color",
                "not-a-color",
                "--apply",
            )

    def test_onboard_tenant_command_rejects_invalid_position(self):
        with self.assertRaises(CommandError):
            call_command(
                "onboard_tenant",
                "--slug",
                "bad-command-position",
                "--name",
                "Bad Command Position",
                "--domain",
                "bad-command-position.example",
                "--position",
                "center",
                "--apply",
            )

    def test_onboard_tenant_command_requires_explicit_mode(self):
        with self.assertRaises(CommandError):
            call_command(
                "onboard_tenant",
                "--slug",
                "command-mode",
                "--name",
                "Command Mode",
                "--domain",
                "command-mode.example",
            )

    def test_onboard_tenant_command_dry_run_keeps_database_unchanged(self):
        output = StringIO()
        call_command(
            "onboard_tenant",
            "--slug",
            "command-dry-run",
            "--name",
            "Command Dry Run",
            "--domain",
            "command-dry-run.example",
            "--dry-run",
            stdout=output,
        )
        self.assertFalse(Tenant.objects.filter(slug="command-dry-run").exists())
        self.assertIn("DRY RUN", output.getvalue())

    def test_onboard_tenant_requires_flag_to_update_existing_tenant(self):
        Tenant.objects.create(slug="command-existing", name="Existing", domain="https://existing.example")
        with self.assertRaises(CommandError):
            call_command(
                "onboard_tenant",
                "--slug",
                "command-existing",
                "--name",
                "Existing Updated",
                "--domain",
                "https://existing.example",
                "--apply",
            )

        call_command(
            "onboard_tenant",
            "--slug",
            "command-existing",
            "--name",
            "Existing Updated",
            "--domain",
            "https://existing.example",
            "--apply",
            "--allow-update-existing",
        )
        self.assertEqual(Tenant.objects.get(slug="command-existing").name, "Existing Updated")

    def test_onboard_tenant_preserves_profile_fields_when_not_explicitly_provided(self):
        tenant = Tenant.objects.create(slug="command-preserve", name="Preserve", domain="https://preserve.example")
        AssistantProfile.objects.create(
            tenant=tenant,
            name="Lívia Preserve",
            primary_color="#123abc",
            launcher_label="Atendimento Preserve",
            placeholder_text="Pergunte aqui",
            is_widget_enabled=False,
        )

        call_command(
            "onboard_tenant",
            "--slug",
            "command-preserve",
            "--name",
            "Preserve Updated",
            "--domain",
            "https://preserve.example",
            "--apply",
            "--allow-update-existing",
        )

        profile = Tenant.objects.get(slug="command-preserve").assistant_profile
        self.assertEqual(profile.primary_color, "#123abc")
        self.assertEqual(profile.launcher_label, "Atendimento Preserve")
        self.assertEqual(profile.placeholder_text, "Pergunte aqui")
        self.assertFalse(profile.is_widget_enabled)



class TenantInstallPackageTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Granimármores Pitondo",
            slug="granimarmores-pitondo",
            domain="www.granimarmorespitondo.com.br/",
            is_active=True,
        )
        AssistantProfile.objects.create(tenant=self.tenant, name="Lívia")
        TenantAllowedOrigin.objects.create(tenant=self.tenant, origin="https://www.granimarmorespitondo.com.br")

    def test_install_page_returns_200_for_existing_tenant(self):
        response = self.client.get("/install/granimarmores-pitondo/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Instalação da Lívia")
        self.assertContains(response, "Granimármores Pitondo")

    def test_install_html_contains_widget_snippet_and_data_tenant(self):
        response = self.client.get("/install/granimarmores-pitondo/")

        self.assertContains(response, "https://livia.smartcontrolbrasil.com.br/widget.js")
        self.assertContains(response, "data-tenant=&quot;granimarmores-pitondo&quot;")
        self.assertContains(response, "data-api-url=&quot;https://livia.smartcontrolbrasil.com.br/api/chat/&quot;")
        self.assertContains(response, " defer&gt;")

    def test_install_html_does_not_contain_secrets(self):
        from integrations.models import TenantWebhookConfig

        TenantWebhookConfig.objects.create(
            tenant=self.tenant,
            name="N8N",
            target_url="https://n8n.example/webhook",
            secret_token="super-secret-token",
        )

        response = self.client.get("/install/granimarmores-pitondo/")
        content = response.content.decode()

        self.assertNotIn("super-secret-token", content)
        self.assertNotIn("secret_token", content)

    def test_install_json_returns_expected_payload(self):
        response = self.client.get("/install/granimarmores-pitondo.json")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["tenant"], "granimarmores-pitondo")
        self.assertEqual(data["name"], "Granimármores Pitondo")
        self.assertTrue(data["is_active"])
        self.assertEqual(data["allowed_origin"], "https://www.granimarmorespitondo.com.br")
        self.assertIn('data-tenant="granimarmores-pitondo"', data["snippet"])
        self.assertIn("widget_config", data)
        self.assertEqual(data["widget_config"]["tenant"], "granimarmores-pitondo")
        self.assertEqual(data["widget_config"]["widget_title"], "Lívia")
        self.assertEqual(data["widget_config"]["primary_color"], "#2563eb")
        self.assertEqual(data["widget_config"]["position"], "bottom_right")
        self.assertTrue(data["widget_config"]["is_widget_enabled"])

    def test_install_json_does_not_contain_secrets(self):
        from integrations.models import TenantWebhookConfig

        TenantWebhookConfig.objects.create(
            tenant=self.tenant,
            name="N8N",
            target_url="https://n8n.example/webhook",
            secret_token="super-secret-token",
        )

        response = self.client.get("/install/granimarmores-pitondo.json")
        payload_text = str(response.json())

        self.assertNotIn("super-secret-token", payload_text)
        self.assertNotIn("secret_token", payload_text)

    def test_install_missing_tenant_returns_404(self):
        html_response = self.client.get("/install/tenant-inexistente/")
        json_response = self.client.get("/install/tenant-inexistente.json")

        self.assertEqual(html_response.status_code, 404)
        self.assertContains(html_response, "Tenant não encontrado", status_code=404)
        self.assertEqual(json_response.status_code, 404)
        self.assertEqual(json_response.json()["error"], "Tenant not found.")

    def test_inactive_tenant_shows_warning(self):
        self.tenant.is_active = False
        self.tenant.save(update_fields=["is_active"])

        response = self.client.get("/install/granimarmores-pitondo/")

        self.assertContains(response, "inativo")
        self.assertContains(response, "NOT_READY")
        self.assertContains(response, "Tenant inativo")

    def test_install_package_warns_when_no_active_origin_exists(self):
        TenantAllowedOrigin.objects.filter(tenant=self.tenant).update(is_active=False)
        response = self.client.get("/install/granimarmores-pitondo.json")
        self.assertIn("Nenhuma origin ativa cadastrada", " ".join(response.json()["warnings"]))

    def test_tenant_admin_exposes_install_url_and_widget_snippet_readonly(self):
        tenant_admin = admin.site._registry[Tenant]

        profile_admin = admin.site._registry[AssistantProfile]

        self.assertIn("install_url", tenant_admin.readonly_fields)
        self.assertIn("widget_snippet_preview", tenant_admin.readonly_fields)
        self.assertIn("/install/granimarmores-pitondo/", tenant_admin.install_url(self.tenant))
        self.assertIn('data-tenant="granimarmores-pitondo"', tenant_admin.widget_snippet_preview(self.tenant))
        self.assertIn("primary_color", profile_admin.list_display)
        self.assertIn("is_widget_enabled", profile_admin.list_filter)

    def test_install_package_reuses_normalized_domain(self):
        package = TenantInstallPackageService().build_for_tenant(self.tenant)

        self.assertEqual(package.allowed_origin, "https://www.granimarmorespitondo.com.br")


class ChatRequestReadinessTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Tenant", slug="tenant")

    @override_settings(LIVIA_CHAT_PROCESSING_TIMEOUT_SECONDS=1)
    def test_readiness_detects_abandoned_processing_request(self):
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            session_id="abandoned",
            request_id=uuid.uuid4(),
            request_fingerprint="f" * 64,
            status=ChatRequest.Status.PROCESSING,
        )
        ChatRequest.objects.filter(pk=chat_request.pk).update(updated_at=timezone.now() - timedelta(seconds=5))

        report = build_database_validation_report()

        self.assertEqual(report.tenant_integrity["abandoned_processing_chat_requests"], 1)

    @override_settings(LIVIA_CHAT_PROCESSING_TIMEOUT_SECONDS=0)
    def test_readiness_detects_invalid_processing_timeout(self):
        report = build_database_validation_report()

        self.assertTrue(report.tenant_integrity["invalid_chat_processing_timeout"])

    @override_settings(DEBUG=False)
    def test_readiness_flags_sqlite_in_production(self):
        report = build_database_validation_report()

        expected = (not settings.DEBUG) and connection.vendor == "sqlite"
        self.assertEqual(report.tenant_integrity["production_sqlite_database"], expected)

    def test_readiness_counts_recent_failed_requests(self):
        ChatRequest.objects.create(
            tenant=self.tenant,
            session_id="failed",
            request_id=uuid.uuid4(),
            request_fingerprint="0" * 64,
            status=ChatRequest.Status.FAILED,
        )

        report = build_database_validation_report()
        self.assertEqual(report.tenant_integrity["recent_failed_chat_requests"], 1)
