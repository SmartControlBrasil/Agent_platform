import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase, override_settings

from agents.application.installations import install_agent
from agents.application.registry import AgentRuntimeRegistry, UnknownAgentRuntime
from agents.domain.runtime import AgentExecutionContext, AgentRequest, AgentResponse
from agents.infrastructure.livia_adapter import LiviaAgentAdapter
from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from tenants.models import Tenant, TenantMembership


TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


class AgentModelTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.other_tenant = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        self.project = Project.objects.create(tenant=self.tenant, name="Site", slug="site")
        self.other_project = Project.objects.create(tenant=self.other_tenant, name="Site", slug="site")
        self.definition = AgentDefinition.objects.create(slug="custom", name="Custom", agent_type="test")
        self.version = AgentVersion.objects.create(
            agent_definition=self.definition,
            version="1.0.0",
            runtime_handler="livia",
            status=AgentVersion.Status.ACTIVE,
        )

    def test_agent_definition_slug_is_unique(self):
        with self.assertRaises(IntegrityError):
            AgentDefinition.objects.create(slug="custom", name="Other", agent_type="test")

    def test_agent_version_unique_per_definition(self):
        with self.assertRaises(IntegrityError):
            AgentVersion.objects.create(agent_definition=self.definition, version="1.0.0", runtime_handler="livia")

    def test_installation_validates_project_tenant(self):
        installation = AgentInstallation(
            tenant=self.tenant,
            project=self.other_project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Broken",
        )

        with self.assertRaises(ValidationError):
            installation.full_clean()

    def test_installation_validates_version_definition(self):
        other_definition = AgentDefinition.objects.create(slug="other", name="Other", agent_type="test")
        other_version = AgentVersion.objects.create(agent_definition=other_definition, version="1.0.0", runtime_handler="other")
        installation = AgentInstallation(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.definition,
            agent_version=other_version,
            name="Broken",
        )

        with self.assertRaises(ValidationError):
            installation.full_clean()

    def test_installation_rejects_plaintext_secrets_in_configuration(self):
        installation = AgentInstallation(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Broken",
            configuration={"api_key": "secret-value"},
        )

        with self.assertRaises(ValidationError):
            installation.full_clean()

    def test_install_agent_service_creates_valid_installation(self):
        installation = install_agent(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.definition,
            agent_version=self.version,
        )

        self.assertEqual(installation.name, "Custom")
        self.assertEqual(installation.tenant, self.tenant)


class AgentRuntimeRegistryTests(TestCase):
    def test_resolves_registered_runtime(self):
        runtime = Mock()
        registry = AgentRuntimeRegistry()
        registry.register("livia", lambda: runtime)

        self.assertIs(registry.resolve("livia"), runtime)
        self.assertEqual(registry.registered_handlers(), ("livia",))

    def test_unknown_runtime_fails_controlled(self):
        registry = AgentRuntimeRegistry()

        with self.assertRaises(UnknownAgentRuntime):
            registry.resolve("missing")


class LiviaAgentAdapterTests(TestCase):
    def test_adapter_implements_runtime_contract_with_legacy_pipeline(self):
        tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        adapter = LiviaAgentAdapter()
        context = AgentExecutionContext(tenant_id=str(tenant.pk), project_id="project", installation_id="installation", session_id="s1")

        with patch("agents.infrastructure.livia_adapter.process_chat_request", return_value={"reply": "Olá"}) as process:
            response = adapter.execute(context, AgentRequest(input="Oi"))

        self.assertIsInstance(response, AgentResponse)
        self.assertEqual(response.output, "Olá")
        self.assertEqual(response.status, "ok")
        process.assert_called_once()

    def test_adapter_requires_session(self):
        response = LiviaAgentAdapter().execute(
            AgentExecutionContext(tenant_id="1", project_id="p", installation_id="i"),
            AgentRequest(input="Oi"),
        )

        self.assertEqual(response.status, "invalid_request")


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class AgentApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="member", password="pass")
        self.tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.other_tenant = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        self.project = Project.objects.create(tenant=self.tenant, name="Site", slug="site")
        self.other_project = Project.objects.create(tenant=self.other_tenant, name="Other", slug="other")
        self.definition = AgentDefinition.objects.create(slug="api-agent", name="API Agent", agent_type="test")
        self.version = AgentVersion.objects.create(
            agent_definition=self.definition,
            version="1.0.0",
            runtime_handler="fake",
            status=AgentVersion.Status.ACTIVE,
        )
        self.installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="API Agent",
        )
        self.other_installation = AgentInstallation.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Other",
        )

    def login(self, tenant=None):
        TenantMembership.objects.create(tenant=tenant or self.tenant, user=self.user, role=TenantMembership.Role.VIEWER)
        self.client.force_login(self.user)

    def test_health_is_public(self):
        response = self.client.get("/api/v1/platform/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service": "agent_platform"})

    def test_lists_require_authentication(self):
        response = self.client.get("/api/v1/projects/")

        self.assertEqual(response.status_code, 401)

    def test_projects_are_tenant_scoped(self):
        self.login()

        response = self.client.get("/api/v1/projects/")

        self.assertEqual(response.status_code, 200)
        slugs = [item["slug"] for item in response.json()["results"]]
        self.assertEqual(slugs, ["site"])

    def test_installations_are_tenant_scoped(self):
        self.login()

        response = self.client.get("/api/v1/agent-installations/")

        self.assertEqual(response.status_code, 200)
        ids = [item["id"] for item in response.json()["results"]]
        self.assertEqual(ids, [str(self.installation.id)])

    def test_execute_blocks_cross_tenant_installation(self):
        self.login()

        response = self.client.post(
            f"/api/v1/agent-installations/{self.other_installation.id}/execute/",
            data=json.dumps({"input": "Oi", "session_id": "s1"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)

    def test_execute_uses_registry_runtime(self):
        self.login()
        runtime = Mock()
        runtime.execute.return_value = AgentResponse(output="done", status="ok", metadata={"trace": "fake"})
        registry = AgentRuntimeRegistry()
        registry.register("fake", lambda: runtime)

        with patch("agents.interfaces.api.runtime_registry", registry):
            response = self.client.post(
                f"/api/v1/agent-installations/{self.installation.id}/execute/",
                data=json.dumps({"input": "Oi", "session_id": "s1"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"], "done")
        runtime.execute.assert_called_once()
