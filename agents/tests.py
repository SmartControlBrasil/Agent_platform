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
from agents.infrastructure.livia_configuration import LiviaConfigurationResolver, normalize_livia_configuration
from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from tenants.models import AssistantProfile, Tenant, TenantMembership


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

    def test_livia_installation_rejects_invalid_runtime_configuration(self):
        definition, _ = AgentDefinition.objects.update_or_create(
            slug="livia",
            defaults={"name": "Lívia", "agent_type": "conversational_sales", "is_active": True},
        )
        version, _ = AgentVersion.objects.update_or_create(
            agent_definition=definition,
            version="1.0.0",
            defaults={"runtime_handler": "livia", "status": AgentVersion.Status.ACTIVE},
        )
        installation = AgentInstallation(
            tenant=self.tenant,
            project=self.project,
            agent_definition=definition,
            agent_version=version,
            name="Lívia",
            configuration={"tone": {"invalid": True}},
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


class LiviaConfigurationResolverTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        self.other_tenant = Tenant.objects.create(name="Other", slug="other")
        self.project = Project.objects.create(tenant=self.tenant, name="Site institucional", slug="site")
        self.other_project = Project.objects.create(tenant=self.other_tenant, name="Other", slug="other")
        self.definition, _ = AgentDefinition.objects.update_or_create(
            slug="livia",
            defaults={"name": "Lívia", "agent_type": "conversational_sales", "is_active": True},
        )
        self.version, _ = AgentVersion.objects.update_or_create(
            agent_definition=self.definition,
            version="legacy-initial",
            defaults={"runtime_handler": "livia", "status": AgentVersion.Status.ACTIVE},
        )
        self.profile = AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia Legacy",
            tone="legado consultivo",
            primary_goal="qualificar legado",
            short_description="Descrição legacy",
        )
        self.installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Lívia",
        )
        self.resolver = LiviaConfigurationResolver()

    def test_uses_installation_configuration_when_all_fields_are_present(self):
        self.installation.configuration = {
            "public_name": "Lívia Comercial",
            "tone": "direto e consultivo",
            "primary_goal": "qualificar oportunidades comerciais",
            "short_description": "Atendimento comercial moderno",
        }

        config = self.resolver.resolve(
            installation=self.installation,
            tenant=self.tenant,
            assistant_profile=self.profile,
        )

        self.assertEqual(config.public_name, "Lívia Comercial")
        self.assertEqual(config.tone, "direto e consultivo")
        self.assertEqual(config.primary_goal, "qualificar oportunidades comerciais")
        self.assertEqual(config.short_description, "Atendimento comercial moderno")

    def test_partial_configuration_falls_back_to_assistant_profile(self):
        self.installation.configuration = {"public_name": "Assistente Comercial"}

        config = self.resolver.resolve(
            installation=self.installation,
            tenant=self.tenant,
            assistant_profile=self.profile,
        )

        self.assertEqual(config.public_name, "Assistente Comercial")
        self.assertEqual(config.tone, "legado consultivo")
        self.assertEqual(config.primary_goal, "qualificar legado")
        self.assertEqual(config.short_description, "Descrição legacy")

    def test_empty_configuration_uses_assistant_profile(self):
        config = self.resolver.resolve(
            installation=self.installation,
            tenant=self.tenant,
            assistant_profile=self.profile,
        )

        self.assertEqual(config.public_name, "Lívia Legacy")
        self.assertEqual(config.tone, "legado consultivo")
        self.assertEqual(config.primary_goal, "qualificar legado")
        self.assertEqual(config.short_description, "Descrição legacy")

    def test_without_assistant_profile_uses_safe_defaults(self):
        config = self.resolver.resolve(
            installation=self.installation,
            tenant=self.tenant,
            assistant_profile=None,
        )

        self.assertEqual(config.public_name, "Lívia")
        self.assertEqual(config.tone, "consultivo, claro e profissional")
        self.assertEqual(config.primary_goal, "qualificar leads")
        self.assertEqual(config.short_description, "")

    def test_tenant_divergence_is_blocked(self):
        with self.assertRaises(ValidationError):
            self.resolver.resolve(
                installation=self.installation,
                tenant=self.other_tenant,
                assistant_profile=None,
            )

    def test_assistant_profile_from_another_tenant_is_blocked(self):
        other_profile = AssistantProfile.objects.create(
            tenant=self.other_tenant,
            name="Outro perfil",
            tone="outro tom",
            primary_goal="outro objetivo",
        )

        with self.assertRaises(ValidationError):
            self.resolver.resolve(
                installation=self.installation,
                tenant=self.tenant,
                assistant_profile=other_profile,
            )

    def test_installation_project_divergence_is_blocked(self):
        divergent = AgentInstallation(
            tenant=self.tenant,
            project=self.other_project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Broken",
        )

        with self.assertRaises(ValidationError):
            self.resolver.resolve(installation=divergent, tenant=self.tenant, assistant_profile=None)

    def test_livia_configuration_normalizes_known_fields_and_rejects_invalid_values(self):
        config = normalize_livia_configuration({"public_name": "  Lívia   Comercial  ", "future_field": {"kept": "generic"}})
        self.assertEqual(config, {"public_name": "Lívia Comercial"})

        with self.assertRaises(ValidationError):
            normalize_livia_configuration({"tone": {"invalid": True}})


class LiviaAgentAdapterTests(TestCase):
    def test_adapter_implements_runtime_contract_with_legacy_pipeline(self):
        tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        project = Project.objects.create(tenant=tenant, name="Site", slug="site")
        definition, _ = AgentDefinition.objects.update_or_create(
            slug="livia",
            defaults={"name": "Lívia", "agent_type": "conversational_sales", "is_active": True},
        )
        version, _ = AgentVersion.objects.update_or_create(
            agent_definition=definition,
            version="legacy-initial",
            defaults={"runtime_handler": "livia", "status": AgentVersion.Status.ACTIVE},
        )
        installation = AgentInstallation.objects.create(
            tenant=tenant,
            project=project,
            agent_definition=definition,
            agent_version=version,
            name="Lívia",
            configuration={"public_name": "Lívia Comercial"},
        )
        adapter = LiviaAgentAdapter()
        context = AgentExecutionContext(
            tenant_id=str(tenant.pk),
            project_id=str(project.pk),
            installation_id=str(installation.pk),
            session_id="s1",
        )

        with patch("agents.infrastructure.livia_adapter.process_chat_request", return_value={"reply": "Olá"}) as process:
            response = adapter.execute(context, AgentRequest(input="Oi"))

        self.assertIsInstance(response, AgentResponse)
        self.assertEqual(response.output, "Olá")
        self.assertEqual(response.status, "ok")
        process.assert_called_once()
        self.assertEqual(process.call_args.kwargs["assistant_profile_override"].name, "Lívia Comercial")

    def test_adapter_requires_session(self):
        response = LiviaAgentAdapter().execute(
            AgentExecutionContext(tenant_id="1", project_id="p", installation_id="i"),
            AgentRequest(input="Oi"),
        )

        self.assertEqual(response.status, "invalid_request")

    def test_adapter_blocks_project_divergence_for_same_installation(self):
        tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        project = Project.objects.create(tenant=tenant, name="Site", slug="site")
        other_project = Project.objects.create(tenant=tenant, name="Outro projeto", slug="outro-projeto")
        definition, _ = AgentDefinition.objects.update_or_create(
            slug="livia",
            defaults={"name": "Lívia", "agent_type": "conversational_sales", "is_active": True},
        )
        version, _ = AgentVersion.objects.update_or_create(
            agent_definition=definition,
            version="legacy-initial",
            defaults={"runtime_handler": "livia", "status": AgentVersion.Status.ACTIVE},
        )
        installation = AgentInstallation.objects.create(
            tenant=tenant,
            project=project,
            agent_definition=definition,
            agent_version=version,
            name="Lívia",
        )

        response = LiviaAgentAdapter().execute(
            AgentExecutionContext(
                tenant_id=str(tenant.pk),
                project_id=str(other_project.pk),
                installation_id=str(installation.pk),
                session_id="s1",
            ),
            AgentRequest(input="Oi"),
        )

        self.assertEqual(response.status, "installation_unavailable")


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

    def test_execute_livia_multi_installation_uses_each_installation_configuration(self):
        self.login()
        project_b = Project.objects.create(tenant=self.tenant, name="Outro projeto", slug="outro-projeto")
        definition, _ = AgentDefinition.objects.update_or_create(
            slug="livia",
            defaults={"name": "Lívia", "agent_type": "conversational_sales", "is_active": True},
        )
        version, _ = AgentVersion.objects.update_or_create(
            agent_definition=definition,
            version="legacy-initial",
            defaults={"runtime_handler": "livia", "status": AgentVersion.Status.ACTIVE},
        )
        installation_a = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=definition,
            agent_version=version,
            name="Lívia",
            configuration={"public_name": "Lívia"},
        )
        installation_b = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=project_b,
            agent_definition=definition,
            agent_version=version,
            name="Assistente Comercial",
            configuration={"public_name": "Assistente Comercial"},
        )

        def fake_process(**kwargs):
            profile = kwargs["assistant_profile_override"]
            return {"reply": profile.name, "assistant_name": profile.name}

        with patch("agents.infrastructure.livia_adapter.process_chat_request", side_effect=fake_process) as process:
            response_a = self.client.post(
                f"/api/v1/agent-installations/{installation_a.id}/execute/",
                data=json.dumps({"input": "Oi", "session_id": "site-session"}),
                content_type="application/json",
            )
            response_b = self.client.post(
                f"/api/v1/agent-installations/{installation_b.id}/execute/",
                data=json.dumps({"input": "Oi", "session_id": "other-session"}),
                content_type="application/json",
            )

        self.assertEqual(response_a.status_code, 200)
        self.assertEqual(response_b.status_code, 200)
        self.assertEqual(response_a.json()["output"], "Lívia")
        self.assertEqual(response_b.json()["output"], "Assistente Comercial")
        self.assertEqual(process.call_args_list[0].kwargs["assistant_profile_override"].name, "Lívia")
        self.assertEqual(process.call_args_list[1].kwargs["assistant_profile_override"].name, "Assistente Comercial")
        self.assertEqual(response_a.json()["metadata"]["runtime_configuration"]["public_name"], "Lívia")
        self.assertEqual(response_b.json()["metadata"]["runtime_configuration"]["public_name"], "Assistente Comercial")
        self.assertEqual(response_a.json()["metadata"]["project_id"], str(self.project.id))
        self.assertEqual(response_b.json()["metadata"]["project_id"], str(project_b.id))
