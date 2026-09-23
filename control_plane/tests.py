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
from tools.application.client_identity import create_service_client_credential
from tools.models import (
    AgentToolBinding,
    ServiceClient,
    ServiceClientAgentAccess,
    ServiceClientCredential,
    ToolDefinition,
    ToolExecution,
    ToolExecutor,
    ToolExecutorCapability,
)


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
        self.prospecting_agent = AgentDefinition.objects.get(slug="prospecting")
        self.prospecting_version = AgentVersion.objects.get(
            agent_definition=self.prospecting_agent,
            version="1.0.0",
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
        self.assertContains(response, "Prospecting Agent")
        self.assertContains(response, "Agent Catalog")
        response = self.client.get(reverse("control_plane:agent_detail", args=[self.agent.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "legacy-initial")
        self.assertContains(response, "livia")

    def test_prospecting_agent_detail_is_available(self):
        response = self.client.get(reverse("control_plane:agent_detail", args=[self.prospecting_agent.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1.0.0")
        self.assertContains(response, "prospecting")

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

    def test_install_prospecting_agent_flow(self):
        response = self.client.post(
            reverse("control_plane:install_agent", args=[self.project_a.pk]),
            {
                "agent_definition": self.prospecting_agent.pk,
                "agent_version": self.prospecting_version.pk,
                "name": "Prospecting A",
                "target_market": "  hospitais  ",
                "target_region": " São Paulo ",
                "target_profile": " hospitais privados ",
                "objective": " identificar oportunidades ",
                "max_results": "50",
            },
        )

        self.assertEqual(response.status_code, 302)
        installation = AgentInstallation.objects.get(name="Prospecting A")
        self.assertEqual(installation.agent_definition, self.prospecting_agent)
        self.assertEqual(
            installation.configuration,
            {
                "target_market": "hospitais",
                "target_region": "São Paulo",
                "target_profile": "hospitais privados",
                "objective": "identificar oportunidades",
                "max_results": 50,
            },
        )

    def test_install_page_switches_configuration_ui_for_prospecting(self):
        response = self.client.get(
            reverse("control_plane:install_agent", args=[self.project_a.pk]),
            {"agent_definition": str(self.prospecting_agent.pk)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Configuração do Prospecting Agent")
        self.assertContains(response, "Target market")
        self.assertContains(response, "Max results")
        self.assertNotContains(response, "Public name")

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

    def test_install_agent_rejects_version_from_another_definition(self):
        other_agent = AgentDefinition.objects.create(slug="other", name="Other", agent_type="test", is_active=True)
        other_version = AgentVersion.objects.create(
            agent_definition=other_agent,
            version="1.0.0",
            runtime_handler="other",
            status=AgentVersion.Status.ACTIVE,
        )

        response = self.client.post(
            reverse("control_plane:install_agent", args=[self.project_a.pk]),
            {
                "agent_definition": self.agent.pk,
                "agent_version": other_version.pk,
                "name": "Incompatível",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Selecione uma versão do agente escolhido.")
        self.assertFalse(AgentInstallation.objects.filter(name="Incompatível").exists())

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

    def test_cross_tenant_prospecting_configuration_update_is_scoped(self):
        prospecting_installation_b = AgentInstallation.objects.create(
            tenant=self.tenant_b,
            project=self.project_b,
            agent_definition=self.prospecting_agent,
            agent_version=self.prospecting_version,
            name="Prospecting B",
            configuration={"target_market": "escolas"},
        )

        response = self.client.post(
            reverse("control_plane:installation_detail", args=[prospecting_installation_b.pk]),
            {"target_market": "hospitais"},
        )

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
                "public_name": "  Lívia   Platform  ",
                "tone": " objetivo ",
                "primary_goal": " atender ",
                "short_description": "  Configuração   segura  ",
                "api_key": "should-not-be-saved",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.installation_a.refresh_from_db()
        self.assertEqual(self.installation_a.configuration["public_name"], "Lívia Platform")
        self.assertEqual(self.installation_a.configuration["tone"], "objetivo")
        self.assertEqual(self.installation_a.configuration["primary_goal"], "atender")
        self.assertEqual(self.installation_a.configuration["short_description"], "Configuração segura")
        self.assertNotIn("api_key", self.installation_a.configuration)
        event = AuditEvent.objects.get(action="agent.configuration.updated", object_id=str(self.installation_a.pk))
        self.assertEqual(event.metadata["installation_id"], str(self.installation_a.pk))
        self.assertEqual(
            sorted(event.metadata["changed_fields"]),
            ["primary_goal", "public_name", "short_description", "tone"],
        )
        self.assertNotIn("Lívia Platform", str(event.metadata))

    def test_prospecting_configuration_update_uses_agent_specific_fields(self):
        installation = AgentInstallation.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_definition=self.prospecting_agent,
            agent_version=self.prospecting_version,
            name="Prospecting A",
            configuration={"target_market": "indústria", "max_results": 20},
        )

        response = self.client.post(
            reverse("control_plane:installation_detail", args=[installation.pk]),
            {
                "target_market": "  hospitais  ",
                "target_region": " São Paulo ",
                "target_profile": " hospitais privados ",
                "objective": " mapear oportunidades ",
                "max_results": "35",
            },
        )

        self.assertEqual(response.status_code, 302)
        installation.refresh_from_db()
        self.assertEqual(
            installation.configuration,
            {
                "target_market": "hospitais",
                "target_region": "São Paulo",
                "target_profile": "hospitais privados",
                "objective": "mapear oportunidades",
                "max_results": 35,
            },
        )
        event = AuditEvent.objects.get(action="agent.configuration.updated", object_id=str(installation.pk))
        self.assertEqual(event.metadata["installation_id"], str(installation.pk))
        self.assertEqual(
            sorted(event.metadata["changed_fields"]),
            ["max_results", "objective", "target_market", "target_profile", "target_region"],
        )
        self.assertNotIn("hospitais", str(event.metadata))

    def test_installation_detail_uses_agent_specific_configuration_ui(self):
        installation = AgentInstallation.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_definition=self.prospecting_agent,
            agent_version=self.prospecting_version,
            name="Prospecting Detail",
        )

        response = self.client.get(reverse("control_plane:installation_detail", args=[installation.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Configuração do Prospecting Agent")
        self.assertContains(response, "Target market")
        self.assertContains(response, "Max results")
        self.assertNotContains(response, "Public name")

    def test_secret_like_configuration_is_rejected_by_model(self):
        self.installation_a.configuration = {"api_key": "secret"}
        with self.assertRaises(ValidationError):
            self.installation_a.full_clean()

    def test_invalid_livia_configuration_is_rejected_by_model(self):
        self.installation_a.configuration = {"tone": {"invalid": True}}
        with self.assertRaises(ValidationError):
            self.installation_a.full_clean()

    def test_mutations_have_csrf_protection(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user_a)
        response = client.post(reverse("control_plane:installation_disable", args=[self.installation_a.pk]))
        self.assertEqual(response.status_code, 403)

    def test_tool_catalog_and_detail(self):
        tool = ToolDefinition.objects.get(slug="prospecting.build_search_plan")

        response = self.client.get(reverse("control_plane:tool_list"))
        detail = self.client.get(reverse("control_plane:tool_detail", args=[tool.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Build Prospecting Search Plan")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "prospecting.build_search_plan")

    def test_installation_detail_shows_tools_section(self):
        response = self.client.get(reverse("control_plane:installation_detail", args=[self.installation_a.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tools")
        self.assertContains(response, "Nenhuma tool vinculada")

    def test_add_tool_flow_is_scoped_and_audited(self):
        installation = AgentInstallation.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_definition=self.prospecting_agent,
            agent_version=self.prospecting_version,
            name="Prospecting A",
        )
        tool = ToolDefinition.objects.get(slug="prospecting.build_search_plan")

        response = self.client.post(
            reverse("control_plane:installation_add_tool", args=[installation.pk]),
            {"tool_definition": tool.pk, "is_enabled": "on", "max_queries": "7"},
        )

        self.assertEqual(response.status_code, 302)
        binding = AgentToolBinding.objects.get(agent_installation=installation, tool_definition=tool)
        self.assertEqual(binding.configuration, {"max_queries": 7})
        self.assertTrue(binding.is_enabled)
        self.assertTrue(AuditEvent.objects.filter(action="tool.bound", object_id=str(binding.pk)).exists())

    def test_google_maps_tool_catalog_detail_and_add_tool_flow(self):
        installation = AgentInstallation.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_definition=self.prospecting_agent,
            agent_version=self.prospecting_version,
            name="Prospecting Maps A",
        )
        tool = ToolDefinition.objects.get(slug="prospecting.search_google_maps")

        catalog = self.client.get(reverse("control_plane:tool_list"))
        detail = self.client.get(reverse("control_plane:tool_detail", args=[tool.pk]))
        add = self.client.post(
            reverse("control_plane:installation_add_tool", args=[installation.pk]),
            {"tool_definition": tool.pk, "is_enabled": "on"},
        )

        self.assertEqual(catalog.status_code, 200)
        self.assertContains(catalog, "Search Google Maps")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "prospecting.search_google_maps")
        self.assertContains(detail, "Executor necessário")
        self.assertEqual(add.status_code, 302)
        binding = AgentToolBinding.objects.get(agent_installation=installation, tool_definition=tool)
        self.assertEqual(binding.configuration, {})
        self.assertTrue(binding.is_enabled)

    def test_add_tool_is_not_available_for_incompatible_livia_installation(self):
        tool = ToolDefinition.objects.get(slug="prospecting.build_search_plan")

        response = self.client.post(
            reverse("control_plane:installation_add_tool", args=[self.installation_a.pk]),
            {"tool_definition": tool.pk, "is_enabled": "on", "max_queries": "7"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AgentToolBinding.objects.filter(agent_installation=self.installation_a).exists())

    def test_tool_binding_enable_disable_and_edit_flow(self):
        installation = AgentInstallation.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_definition=self.prospecting_agent,
            agent_version=self.prospecting_version,
            name="Prospecting A",
        )
        tool = ToolDefinition.objects.get(slug="prospecting.build_search_plan")
        binding = AgentToolBinding.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_installation=installation,
            tool_definition=tool,
            configuration={"max_queries": 4},
        )

        disable = self.client.post(reverse("control_plane:tool_binding_disable", args=[binding.pk]))
        binding.refresh_from_db()
        enable = self.client.post(reverse("control_plane:tool_binding_enable", args=[binding.pk]))
        binding.refresh_from_db()
        edit = self.client.post(
            reverse("control_plane:tool_binding_edit", args=[binding.pk]),
            {"tool_definition": tool.pk, "is_enabled": "on", "max_queries": "9"},
        )
        binding.refresh_from_db()

        self.assertEqual(disable.status_code, 302)
        self.assertEqual(enable.status_code, 302)
        self.assertEqual(edit.status_code, 302)
        self.assertTrue(binding.is_enabled)
        self.assertEqual(binding.configuration, {"max_queries": 9})
        self.assertTrue(AuditEvent.objects.filter(action="tool.disabled", object_id=str(binding.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="tool.enabled", object_id=str(binding.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="tool.configuration.updated", object_id=str(binding.pk)).exists())

    def test_tool_binding_cross_tenant_is_404(self):
        installation = AgentInstallation.objects.create(
            tenant=self.tenant_b,
            project=self.project_b,
            agent_definition=self.prospecting_agent,
            agent_version=self.prospecting_version,
            name="Prospecting B",
        )
        tool = ToolDefinition.objects.get(slug="prospecting.build_search_plan")
        binding = AgentToolBinding.objects.create(
            tenant=self.tenant_b,
            project=self.project_b,
            agent_installation=installation,
            tool_definition=tool,
        )

        response = self.client.post(reverse("control_plane:tool_binding_disable", args=[binding.pk]))

        self.assertEqual(response.status_code, 404)


    def test_executor_detail_can_assign_and_toggle_generic_capability(self):
        executor = ToolExecutor.objects.create(
            tenant=self.tenant_a,
            name="Browser Executor A",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="browser-a",
        )
        tool = ToolDefinition.objects.get(slug="prospecting.search_google_maps")

        detail = self.client.get(reverse("control_plane:executor_detail", args=[executor.pk]))
        add = self.client.post(
            reverse("control_plane:executor_capability_add", args=[executor.pk]),
            {"tool_definition": tool.pk},
        )
        capability = ToolExecutorCapability.objects.get(executor=executor, tool_definition=tool)
        disable = self.client.post(reverse("control_plane:executor_capability_disable", args=[capability.pk]))
        capability.refresh_from_db()
        self.assertFalse(capability.is_enabled)
        enable = self.client.post(reverse("control_plane:executor_capability_enable", args=[capability.pk]))
        capability.refresh_from_db()

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "prospecting.search_google_maps")
        self.assertEqual(add.status_code, 302)
        self.assertEqual(disable.status_code, 302)
        self.assertEqual(enable.status_code, 302)
        self.assertTrue(capability.is_enabled)

    def test_google_maps_tool_execution_detail_shows_summary_preview(self):
        installation = AgentInstallation.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_definition=self.prospecting_agent,
            agent_version=self.prospecting_version,
            name="Prospecting Maps Detail",
        )
        tool = ToolDefinition.objects.get(slug="prospecting.search_google_maps")
        binding = AgentToolBinding.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_installation=installation,
            tool_definition=tool,
        )
        executor = ToolExecutor.objects.create(
            tenant=self.tenant_a,
            name="Browser Detail",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="browser-detail",
        )
        execution = ToolExecution.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_installation=installation,
            tool_binding=binding,
            tool_definition=tool,
            executor=executor,
            execution_mode=tool.execution_mode,
            status=ToolExecution.Status.SUCCEEDED,
            request_payload={"input": {"queries": ["hospital privado São Paulo"], "max_results": 10}},
            result_payload={
                "schema_version": 1,
                "status": "completed",
                "businesses": [{"name": "Hospital A", "category": None, "address": "Rua A", "source_query": "hospital privado São Paulo"}],
                "stats": {"duration_ms": 321},
            },
        )

        response = self.client.get(reverse("control_plane:tool_execution_detail", args=[execution.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Resumo do resultado")
        self.assertContains(response, "hospital privado São Paulo")
        self.assertContains(response, "Hospital A")
        self.assertContains(response, "321 ms")

    def test_tool_execution_control_plane_list_and_detail_are_tenant_scoped(self):
        tool = ToolDefinition.objects.get(slug="prospecting.build_search_plan")
        binding_a = AgentToolBinding.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_installation=self.installation_a,
            tool_definition=tool,
        )
        binding_b = AgentToolBinding.objects.create(
            tenant=self.tenant_b,
            project=self.project_b,
            agent_installation=self.installation_b,
            tool_definition=tool,
        )
        execution_a = ToolExecution.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_installation=self.installation_a,
            tool_binding=binding_a,
            tool_definition=tool,
            execution_mode=tool.execution_mode,
        )
        execution_b = ToolExecution.objects.create(
            tenant=self.tenant_b,
            project=self.project_b,
            agent_installation=self.installation_b,
            tool_binding=binding_b,
            tool_definition=tool,
            execution_mode=tool.execution_mode,
        )

        response = self.client.get(reverse("control_plane:tool_execution_list"))
        detail_a = self.client.get(reverse("control_plane:tool_execution_detail", args=[execution_a.pk]))
        detail_b = self.client.get(reverse("control_plane:tool_execution_detail", args=[execution_b.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(execution_a.tool_definition.name))
        self.assertNotContains(response, str(execution_b.pk))
        self.assertEqual(detail_a.status_code, 200)
        self.assertContains(detail_a, str(execution_a.pk))
        self.assertEqual(detail_b.status_code, 404)


class ControlPlaneServiceClientTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user_a = User.objects.create_user(username="service-client-admin", password="pw")
        self.viewer = User.objects.create_user(username="service-client-viewer", password="pw")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a", domain="a.example")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b", domain="b.example")
        TenantMembership.objects.create(
            tenant=self.tenant_a,
            user=self.user_a,
            role=TenantMembership.Role.TENANT_ADMIN,
        )
        TenantMembership.objects.create(
            tenant=self.tenant_a,
            user=self.viewer,
            role=TenantMembership.Role.VIEWER,
        )
        self.project_a = Project.objects.create(tenant=self.tenant_a, name="Project A", slug="project-a")
        self.project_b = Project.objects.create(tenant=self.tenant_b, name="Project B", slug="project-b")
        self.prospecting_agent = AgentDefinition.objects.get(slug="prospecting")
        self.prospecting_version = AgentVersion.objects.get(agent_definition=self.prospecting_agent, version="1.0.0")
        self.prospecting_installation = AgentInstallation.objects.create(
            tenant=self.tenant_a,
            project=self.project_a,
            agent_definition=self.prospecting_agent,
            agent_version=self.prospecting_version,
            name="Service Client Prospecting A",
        )
        self.client.force_login(self.user_a)

    def test_service_client_create_credential_access_rotate_and_revoke(self):
        create_response = self.client.post(
            reverse("control_plane:service_client_list"),
            {"tenant_id": self.tenant_a.pk, "name": "Smart Sales", "slug": "smart-sales"},
        )
        service_client = ServiceClient.objects.get(slug="smart-sales")
        detail_url = reverse("control_plane:service_client_detail", args=[service_client.pk])

        credential_response = self.client.post(reverse("control_plane:service_client_credential_create", args=[service_client.pk]))
        credential = ServiceClientCredential.objects.get(service_client=service_client)
        grant_response = self.client.post(
            reverse("control_plane:service_client_access_grant", args=[service_client.pk]),
            {"agent_installation": self.prospecting_installation.pk},
        )
        access = ServiceClientAgentAccess.objects.get(service_client=service_client, agent_installation=self.prospecting_installation)
        rotate_response = self.client.post(reverse("control_plane:service_client_credential_rotate", args=[credential.pk]))
        revoke_response = self.client.post(reverse("control_plane:service_client_access_revoke", args=[access.pk]))

        self.assertEqual(create_response.status_code, 302)
        self.assertEqual(create_response["Location"], detail_url)
        self.assertEqual(credential_response.status_code, 200)
        self.assertContains(credential_response, "apc_")
        self.assertEqual(grant_response.status_code, 302)
        self.assertTrue(access.is_active)
        self.assertEqual(rotate_response.status_code, 200)
        self.assertContains(rotate_response, "apc_")
        self.assertEqual(ServiceClientCredential.objects.filter(service_client=service_client).count(), 2)
        self.assertEqual(revoke_response.status_code, 302)
        access.refresh_from_db()
        self.assertFalse(access.is_active)
        self.assertTrue(AuditEvent.objects.filter(action="service_client.created", object_id=str(service_client.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="service_client.access.revoked", object_id=str(access.pk)).exists())

    def test_service_client_control_plane_is_tenant_scoped_and_viewer_cannot_mutate(self):
        service_client = ServiceClient.objects.create(tenant=self.tenant_b, name="Tenant B Client", slug="tenant-b-client")
        create_service_client_credential(service_client=service_client)

        list_response = self.client.get(reverse("control_plane:service_client_list"))
        detail_response = self.client.get(reverse("control_plane:service_client_detail", args=[service_client.pk]))
        self.client.force_login(self.viewer)
        viewer_create = self.client.post(
            reverse("control_plane:service_client_list"),
            {"tenant_id": self.tenant_a.pk, "name": "No", "slug": "no"},
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertNotContains(list_response, "Tenant B Client")
        self.assertEqual(detail_response.status_code, 404)
        self.assertEqual(viewer_create.status_code, 403)
