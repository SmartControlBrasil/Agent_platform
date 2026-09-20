from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from agents.application.installations import (
    disable_agent_installation,
    enable_agent_installation,
    install_agent,
)
from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from audit.models import AuditEvent
from projects.models import Project
from tenants.models import Tenant, TenantMembership


class ControlPlaneTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user_a = User.objects.create_user(username="user-a", password="pw")
        self.user_b = User.objects.create_user(username="user-b", password="pw")
        self.viewer = User.objects.create_user(username="viewer", password="pw")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a", domain="a.example")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b", domain="b.example")
        TenantMembership.objects.create(
            tenant=self.tenant_a,
            user=self.user_a,
            role=TenantMembership.Role.TENANT_ADMIN,
        )
        TenantMembership.objects.create(
            tenant=self.tenant_b,
            user=self.user_b,
            role=TenantMembership.Role.TENANT_ADMIN,
        )
        TenantMembership.objects.create(
            tenant=self.tenant_a,
            user=self.viewer,
            role=TenantMembership.Role.VIEWER,
        )
        self.project_a = Project.objects.create(tenant=self.tenant_a, name="Project A", slug="project-a")
        self.project_b = Project.objects.create(tenant=self.tenant_b, name="Project B", slug="project-b")
        self.agent, _ = AgentDefinition.objects.update_or_create(
            slug="livia",
            defaults={
                "name": "Lívia",
                "description": "Conversational Sales",
                "agent_type": "conversational_sales",
                "is_active": True,
            },
        )
        self.version, _ = AgentVersion.objects.update_or_create(
            agent_definition=self.agent,
            version="legacy-initial",
            defaults={
                "runtime_handler": "livia",
                "status": AgentVersion.Status.ACTIVE,
            },
        )
        self.installation_a = AgentInstallation.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_definition=self.agent,
            agent_version=self.version,
            name="Lívia A",
            configuration={"public_name": "Lívia"},
        )
        self.installation_b = AgentInstallation.objects.create(
            tenant=self.tenant_b,
            project=self.project_b,
            agent_definition=self.agent,
            agent_version=self.version,
            name="Lívia B",
        )
        self.client.force_login(self.user_a)

    def test_dashboard_requires_authentication(self):
        client = Client()
        response = client.get(reverse("control_plane:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_dashboard_authenticated(self):
        response = self.client.get(reverse("control_plane:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Agent Platform")
        self.assertContains(response, "Tenants acessíveis")

    def test_tenant_list_is_scoped(self):
        response = self.client.get(reverse("control_plane:tenant_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tenant A")
        self.assertNotContains(response, "Tenant B")

    def test_tenant_detail_is_scoped(self):
        response = self.client.get(reverse("control_plane:tenant_detail", args=[self.tenant_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_project_create_is_limited_to_manageable_tenants(self):
        response = self.client.post(
            reverse("control_plane:project_create"),
            {
                "tenant": self.tenant_a.pk,
                "name": "New Project",
                "slug": "new-project",
                "description": "Created from control plane",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(slug="new-project")
        self.assertEqual(project.tenant, self.tenant_a)
        self.assertTrue(AuditEvent.objects.filter(action="project.created", object_id=str(project.pk)).exists())

    def test_project_create_rejects_tenant_outside_membership(self):
        response = self.client.post(
            reverse("control_plane:project_create"),
            {
                "tenant": self.tenant_b.pk,
                "name": "Cross Project",
                "slug": "cross-project",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Project.objects.filter(slug="cross-project").exists())

    def test_project_create_requires_manage_permission(self):
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("control_plane:project_create"))
        self.assertEqual(response.status_code, 403)

    def test_project_detail_is_scoped(self):
        response = self.client.get(reverse("control_plane:project_detail", args=[self.project_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_agent_catalog_and_detail(self):
        response = self.client.get(reverse("control_plane:agent_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lívia")
        self.assertContains(response, "Agent Catalog")
        response = self.client.get(reverse("control_plane:agent_detail", args=[self.agent.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "legacy-initial")
        self.assertContains(response, "livia")

    def test_install_agent_flow(self):
        response = self.client.post(
            reverse("control_plane:install_agent", args=[self.project_a.pk]),
            {
                "agent_definition": self.agent.pk,
                "agent_version": self.version.pk,
                "name": "Lívia Comercial",
                "public_name": "Lívia",
                "tone": "consultivo",
                "primary_goal": "qualificar leads",
                "short_description": "Atendimento comercial",
            },
        )
        self.assertEqual(response.status_code, 302)
        installation = AgentInstallation.objects.get(name="Lívia Comercial")
        self.assertEqual(installation.tenant, self.tenant_a)
        self.assertEqual(installation.configuration["public_name"], "Lívia")
        self.assertTrue(AuditEvent.objects.filter(action="agent.installed", object_id=str(installation.pk)).exists())

    def test_install_agent_rejects_inactive_project(self):
        self.project_a.is_active = False
        self.project_a.save(update_fields=["is_active"])
        response = self.client.post(
            reverse("control_plane:install_agent", args=[self.project_a.pk]),
            {"agent_definition": self.agent.pk, "agent_version": self.version.pk, "name": "Inactive"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AgentInstallation.objects.filter(name="Inactive").exists())

    def test_install_agent_rejects_inactive_agent_and_version(self):
        self.agent.is_active = False
        self.agent.save(update_fields=["is_active"])
        response = self.client.post(
            reverse("control_plane:install_agent", args=[self.project_a.pk]),
            {"agent_definition": self.agent.pk, "agent_version": self.version.pk, "name": "Inactive Agent"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AgentInstallation.objects.filter(name="Inactive Agent").exists())

        draft_agent = AgentDefinition.objects.create(slug="draft", name="Draft", agent_type="test")
        draft_version = AgentVersion.objects.create(
            agent_definition=draft_agent,
            version="draft",
            runtime_handler="livia",
            status=AgentVersion.Status.DRAFT,
        )
        response = self.client.post(
            reverse("control_plane:install_agent", args=[self.project_a.pk]),
            {"agent_definition": draft_agent.pk, "agent_version": draft_version.pk, "name": "Draft Agent"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AgentInstallation.objects.filter(name="Draft Agent").exists())

    def test_install_service_rejects_inactive_tenant(self):
        self.tenant_a.is_active = False
        self.tenant_a.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            install_agent(
                tenant=self.tenant_a,
                project=self.project_a,
                agent_definition=self.agent,
                agent_version=self.version,
                name="Inactive Tenant",
            )

    def test_cross_tenant_installation_attempt_is_rejected(self):
        response = self.client.post(
            reverse("control_plane:install_agent", args=[self.project_b.pk]),
            {"agent_definition": self.agent.pk, "agent_version": self.version.pk, "name": "Cross"},
        )
        self.assertEqual(response.status_code, 404)

        with self.assertRaises(ValidationError):
            install_agent(
                tenant=self.tenant_a,
                project=self.project_b,
                agent_definition=self.agent,
                agent_version=self.version,
                name="Cross",
            )

    def test_installation_detail_is_scoped(self):
        response = self.client.get(reverse("control_plane:installation_detail", args=[self.installation_b.pk]))
        self.assertEqual(response.status_code, 404)

    def test_enable_disable_installation(self):
        response = self.client.post(reverse("control_plane:installation_disable", args=[self.installation_a.pk]))
        self.assertEqual(response.status_code, 302)
        self.installation_a.refresh_from_db()
        self.assertFalse(self.installation_a.is_enabled)
        self.assertTrue(AuditEvent.objects.filter(action="agent.disabled", object_id=str(self.installation_a.pk)).exists())

        response = self.client.post(reverse("control_plane:installation_enable", args=[self.installation_a.pk]))
        self.assertEqual(response.status_code, 302)
        self.installation_a.refresh_from_db()
        self.assertTrue(self.installation_a.is_enabled)
        self.assertTrue(AuditEvent.objects.filter(action="agent.enabled", object_id=str(self.installation_a.pk)).exists())

    def test_enable_rejects_inactive_project(self):
        self.project_a.is_active = False
        self.project_a.save(update_fields=["is_active"])
        self.installation_a.is_enabled = False
        self.installation_a.save(update_fields=["is_enabled"])
        with self.assertRaises(ValidationError):
            enable_agent_installation(installation=self.installation_a)

    def test_disable_keeps_installation_visible(self):
        disable_agent_installation(installation=self.installation_a)
        response = self.client.get(reverse("control_plane:installation_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lívia A")
        self.assertContains(response, "disabled")

    def test_configuration_update_safe_fields_only(self):
        response = self.client.post(
            reverse("control_plane:installation_detail", args=[self.installation_a.pk]),
            {
                "public_name": "Lívia Platform",
                "tone": "objetivo",
                "primary_goal": "atender",
                "short_description": "Configuração segura",
                "api_key": "should-not-be-saved",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.installation_a.refresh_from_db()
        self.assertEqual(self.installation_a.configuration["public_name"], "Lívia Platform")
        self.assertNotIn("api_key", self.installation_a.configuration)
        self.assertTrue(
            AuditEvent.objects.filter(action="agent.configuration.updated", object_id=str(self.installation_a.pk)).exists()
        )

    def test_secret_like_configuration_is_rejected_by_model(self):
        self.installation_a.configuration = {"api_key": "secret"}
        with self.assertRaises(ValidationError):
            self.installation_a.full_clean()

    def test_mutations_have_csrf_protection(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user_a)
        response = client.post(reverse("control_plane:installation_disable", args=[self.installation_a.pk]))
        self.assertEqual(response.status_code, 403)
