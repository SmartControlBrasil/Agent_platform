import json
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from audit.models import AuditEvent
from projects.models import Project
from tenants.models import Tenant, TenantMembership
from tools.application.execution import ToolExecutionError, execute_tool
from tools.application.lifecycle import (
    ToolExecutionLifecycleError,
    claim_tool_execution,
    complete_tool_execution,
    fail_tool_execution,
    transition_execution,
)
from tools.application.registry import ToolRuntimeRegistry, UnknownToolRuntime
from tools.domain.runtime import ToolResult
from tools.infrastructure.prospecting_build_search_plan import (
    ProspectingBuildSearchPlanTool,
    normalize_build_search_plan_configuration,
)
from tools.models import AgentToolBinding, ToolDefinition, ToolExecution, ToolExecutor, ToolExecutorCapability


TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


class ToolTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.other_tenant = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        self.project = Project.objects.create(tenant=self.tenant, name="Project A", slug="project-a")
        self.other_project = Project.objects.create(tenant=self.other_tenant, name="Project B", slug="project-b")
        self.agent = AgentDefinition.objects.get(slug="prospecting")
        self.version = AgentVersion.objects.get(agent_definition=self.agent, version="1.0.0")
        self.installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.agent,
            agent_version=self.version,
            name="Prospecting A",
            configuration={"target_market": "hospitais", "target_region": "São Paulo"},
        )
        self.other_installation = AgentInstallation.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_definition=self.agent,
            agent_version=self.version,
            name="Prospecting B",
        )
        self.tool = ToolDefinition.objects.get(slug="prospecting.build_search_plan")
        self.delegated_tool = ToolDefinition.objects.get(slug="prospecting.external_search_probe")

    def bind(self, installation=None, tool=None, enabled=True, configuration=None):
        installation = installation or self.installation
        return AgentToolBinding.objects.create(
            tenant=installation.tenant,
            project=installation.project,
            agent_installation=installation,
            tool_definition=tool or self.tool,
            is_enabled=enabled,
            configuration=configuration or {"max_queries": 3},
        )


class ToolModelTests(ToolTestCase):
    def test_bootstrap_tool_exists(self):
        self.assertEqual(self.tool.runtime_handler, "prospecting_build_search_plan")
        self.assertEqual(self.tool.execution_mode, ToolDefinition.ExecutionMode.LOCAL)
        self.assertTrue(self.tool.is_active)

    def test_delegated_probe_tool_exists_without_local_runtime(self):
        self.assertEqual(self.delegated_tool.name, "External Search Probe")
        self.assertEqual(self.delegated_tool.execution_mode, ToolDefinition.ExecutionMode.DELEGATED)
        self.assertEqual(self.delegated_tool.runtime_handler, "")
        self.assertTrue(self.delegated_tool.is_active)

    def test_binding_validates_installation_scope(self):
        binding = AgentToolBinding(
            tenant=self.tenant,
            project=self.other_project,
            agent_installation=self.installation,
            tool_definition=self.tool,
        )

        with self.assertRaises(ValidationError):
            binding.full_clean()

    def test_binding_rejects_plaintext_secret_configuration(self):
        binding = AgentToolBinding(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            tool_definition=self.tool,
            configuration={"api_key": "secret"},
        )

        with self.assertRaises(ValidationError):
            binding.full_clean()

    def test_duplicate_binding_is_rejected(self):
        self.bind()

        with self.assertRaises(ValidationError):
            self.bind()


class ToolRuntimeRegistryTests(TestCase):
    def test_resolves_registered_runtime(self):
        runtime = Mock()
        registry = ToolRuntimeRegistry()
        registry.register("handler", lambda: runtime)

        self.assertIs(registry.resolve("handler"), runtime)
        self.assertEqual(registry.registered_handlers(), ("handler",))

    def test_unknown_runtime_fails_controlled(self):
        with self.assertRaises(UnknownToolRuntime):
            ToolRuntimeRegistry().resolve("missing")


class ProspectingBuildSearchPlanToolTests(TestCase):
    def test_configuration_normalizes_max_queries(self):
        self.assertEqual(normalize_build_search_plan_configuration({"max_queries": "5"}), {"max_queries": 5})

        with self.assertRaises(ValidationError):
            normalize_build_search_plan_configuration({"max_queries": 0})
        with self.assertRaises(ValidationError):
            normalize_build_search_plan_configuration({"max_queries": "abc"})

    def test_execute_builds_deterministic_queries(self):
        from tools.domain.runtime import ToolExecutionContext, ToolRequest

        tool = ProspectingBuildSearchPlanTool()
        result = tool.execute(
            ToolExecutionContext(
                tenant_id="1",
                project_id="p",
                installation_id="i",
                tool_binding_id="b",
            ),
            ToolRequest(
                input={
                    "target_market": "hospitais",
                    "target_region": "São Paulo",
                    "target_profile": "hospitais privados",
                    "objective": "identificar oportunidades",
                },
                metadata={"binding_configuration": {"max_queries": 4}},
            ),
        )

        self.assertEqual(result.status, "planned")
        self.assertEqual(len(result.output["queries"]), 4)
        self.assertEqual(result.output["queries"][0], "hospitais privados São Paulo")
        self.assertEqual(result.metadata["max_queries"], 4)


class ToolExecutionServiceTests(ToolTestCase):
    def test_execute_tool_success_records_audit_event(self):
        self.bind(configuration={"max_queries": 2})

        result = execute_tool(
            installation=self.installation,
            tool_slug="prospecting.build_search_plan",
            input={"target_market": "hospitais", "target_region": "São Paulo"},
        )

        self.assertEqual(result.status, "planned")
        self.assertEqual(len(result.output["queries"]), 2)
        execution = ToolExecution.objects.get(tool_binding__agent_installation=self.installation)
        self.assertEqual(execution.execution_mode, ToolDefinition.ExecutionMode.LOCAL)
        self.assertEqual(execution.status, ToolExecution.Status.SUCCEEDED)
        self.assertEqual(execution.result_payload["status"], "planned")
        self.assertTrue(AuditEvent.objects.filter(action="tool.executed").exists())
        self.assertTrue(AuditEvent.objects.filter(action="tool.execution.created").exists())
        self.assertTrue(AuditEvent.objects.filter(action="tool.execution.succeeded").exists())

    def test_execute_requires_binding(self):
        with self.assertRaises(ToolExecutionError):
            execute_tool(installation=self.installation, tool_slug="prospecting.build_search_plan", input={})

    def test_execute_blocks_disabled_binding(self):
        self.bind(enabled=False)

        with self.assertRaises(ToolExecutionError):
            execute_tool(installation=self.installation, tool_slug="prospecting.build_search_plan", input={})

    def test_execute_blocks_inactive_tool_and_installation_scope(self):
        self.bind()
        self.tool.is_active = False
        self.tool.save(update_fields=["is_active"] )

        with self.assertRaises(ToolExecutionError):
            execute_tool(installation=self.installation, tool_slug="prospecting.build_search_plan", input={})

        self.tool.is_active = True
        self.tool.save(update_fields=["is_active"] )
        self.installation.is_enabled = False
        self.installation.save(update_fields=["is_enabled"] )

        with self.assertRaises(ToolExecutionError):
            execute_tool(installation=self.installation, tool_slug="prospecting.build_search_plan", input={})

    def test_execute_blocks_inactive_tenant_or_project(self):
        self.bind()
        self.project.is_active = False
        self.project.save(update_fields=["is_active"] )

        with self.assertRaises(ToolExecutionError):
            execute_tool(installation=self.installation, tool_slug="prospecting.build_search_plan", input={})

        self.project.is_active = True
        self.project.save(update_fields=["is_active"] )
        self.tenant.is_active = False
        self.tenant.save(update_fields=["is_active"] )

        with self.assertRaises(ToolExecutionError):
            execute_tool(installation=self.installation, tool_slug="prospecting.build_search_plan", input={})

    def test_unknown_runtime_is_not_dynamic_imported(self):
        tool = ToolDefinition.objects.create(
            slug="prospecting.unknown",
            name="Unknown",
            category="prospecting",
            runtime_handler="missing",
            is_active=True,
        )
        self.bind(tool=tool)

        with self.assertRaises(UnknownToolRuntime):
            execute_tool(installation=self.installation, tool_slug="prospecting.unknown", input={})

    def test_custom_registry_is_used(self):
        self.bind()
        runtime = Mock()
        runtime.execute.return_value = ToolResult(status="custom", output={"ok": True})
        registry = ToolRuntimeRegistry()
        registry.register("prospecting_build_search_plan", lambda: runtime)

        result = execute_tool(
            installation=self.installation,
            tool_slug="prospecting.build_search_plan",
            input={},
            registry=registry,
        )

        self.assertEqual(result.status, "custom")
        runtime.execute.assert_called_once()

    def test_idempotency_reuses_existing_local_execution_result(self):
        self.bind(configuration={"max_queries": 2})

        first = execute_tool(
            installation=self.installation,
            tool_slug="prospecting.build_search_plan",
            input={"target_market": "hospitais"},
            idempotency_key="same-run",
        )
        second = execute_tool(
            installation=self.installation,
            tool_slug="prospecting.build_search_plan",
            input={"target_market": "hospitais"},
            idempotency_key="same-run",
        )

        self.assertEqual(first.status, "planned")
        self.assertEqual(second.status, "planned")
        self.assertEqual(ToolExecution.objects.filter(idempotency_key="same-run").count(), 1)

    def test_status_transitions_reject_invalid_terminal_reopen(self):
        binding = self.bind()
        execution = ToolExecution.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            tool_binding=binding,
            tool_definition=self.tool,
            execution_mode=ToolDefinition.ExecutionMode.LOCAL,
        )

        transition_execution(execution, ToolExecution.Status.RUNNING)
        transition_execution(execution, ToolExecution.Status.SUCCEEDED, result_payload={"ok": True})

        with self.assertRaises(ToolExecutionLifecycleError):
            transition_execution(execution, ToolExecution.Status.RUNNING)

    def test_delegated_execute_creates_dispatched_execution_without_runtime(self):
        self.bind(tool=self.delegated_tool)
        registry = ToolRuntimeRegistry()

        result = execute_tool(
            installation=self.installation,
            tool_slug="prospecting.external_search_probe",
            input={"query": "hospitais"},
            registry=registry,
        )

        execution = ToolExecution.objects.get(tool_definition=self.delegated_tool)
        self.assertEqual(result.status, "dispatched")
        self.assertEqual(execution.status, ToolExecution.Status.DISPATCHED)
        self.assertEqual(execution.execution_mode, ToolDefinition.ExecutionMode.DELEGATED)
        self.assertTrue(AuditEvent.objects.filter(action="tool.execution.dispatched", object_id=str(execution.pk)).exists())

    def test_delegated_claim_complete_and_fail_services(self):
        self.bind(tool=self.delegated_tool)
        execute_tool(
            installation=self.installation,
            tool_slug="prospecting.external_search_probe",
            input={"query": "hospitais"},
        )
        execution = ToolExecution.objects.get(tool_definition=self.delegated_tool)
        executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="Test Browser Executor",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="test-browser-a",
        )
        ToolExecutorCapability.objects.create(executor=executor, tool_definition=self.delegated_tool)

        claimed = claim_tool_execution(executor=executor, execution=execution)
        completed = complete_tool_execution(executor=executor, execution=claimed, result={"items": ["ok"]})

        self.assertEqual(claimed.status, ToolExecution.Status.RUNNING)
        self.assertEqual(completed.status, ToolExecution.Status.SUCCEEDED)
        self.assertEqual(completed.result_payload, {"items": ["ok"]})
        self.assertTrue(AuditEvent.objects.filter(action="tool.execution.claimed", object_id=str(execution.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(action="tool.execution.succeeded", object_id=str(execution.pk)).exists())

    def test_fail_execution_sanitizes_error_and_requires_owner(self):
        self.bind(tool=self.delegated_tool)
        execute_tool(installation=self.installation, tool_slug="prospecting.external_search_probe", input={})
        execution = ToolExecution.objects.get(tool_definition=self.delegated_tool)
        owner = ToolExecutor.objects.create(
            tenant=self.tenant, name="Owner", executor_type=ToolExecutor.ExecutorType.WORKER, public_id="owner"
        )
        other = ToolExecutor.objects.create(
            tenant=self.tenant, name="Other", executor_type=ToolExecutor.ExecutorType.WORKER, public_id="other"
        )
        ToolExecutorCapability.objects.create(executor=owner, tool_definition=self.delegated_tool)
        ToolExecutorCapability.objects.create(executor=other, tool_definition=self.delegated_tool)
        claim_tool_execution(executor=owner, execution=execution)

        with self.assertRaises(ToolExecutionLifecycleError):
            complete_tool_execution(executor=other, execution=execution, result={})

        failed = fail_tool_execution(
            executor=owner,
            execution=execution,
            error_code="token leaked",
            error_message="api_key should not appear",
        )

        self.assertEqual(failed.status, ToolExecution.Status.FAILED)
        self.assertEqual(failed.error_code, "[sanitized]")
        self.assertEqual(failed.error_message, "[sanitized]")

    def test_unauthorized_inactive_cross_tenant_and_double_claim_are_blocked(self):
        self.bind(tool=self.delegated_tool)
        execute_tool(installation=self.installation, tool_slug="prospecting.external_search_probe", input={})
        execution = ToolExecution.objects.get(tool_definition=self.delegated_tool)
        executor_a = ToolExecutor.objects.create(
            tenant=self.tenant, name="Executor A", executor_type=ToolExecutor.ExecutorType.WORKER, public_id="executor-a"
        )
        executor_b = ToolExecutor.objects.create(
            tenant=self.other_tenant, name="Executor B", executor_type=ToolExecutor.ExecutorType.WORKER, public_id="executor-b"
        )

        with self.assertRaises(ToolExecutionLifecycleError):
            claim_tool_execution(executor=executor_a, execution=execution)

        ToolExecutorCapability.objects.create(executor=executor_a, tool_definition=self.delegated_tool)
        ToolExecutorCapability.objects.create(executor=executor_b, tool_definition=self.delegated_tool)
        executor_a.is_active = False
        executor_a.save(update_fields=["is_active"])
        with self.assertRaises(ToolExecutionLifecycleError):
            claim_tool_execution(executor=executor_a, execution=execution)

        executor_a.is_active = True
        executor_a.save(update_fields=["is_active"])
        with self.assertRaises(ToolExecutionLifecycleError):
            claim_tool_execution(executor=executor_b, execution=execution)

        claim_tool_execution(executor=executor_a, execution=execution)
        with self.assertRaises(ToolExecutionLifecycleError):
            claim_tool_execution(executor=executor_a, execution=execution)

    def test_expired_execution_rejects_claim_and_marks_expired(self):
        self.bind(tool=self.delegated_tool)
        execute_tool(
            installation=self.installation,
            tool_slug="prospecting.external_search_probe",
            input={},
            expires_at=timezone.now() - timezone.timedelta(minutes=1),
        )
        execution = ToolExecution.objects.get(tool_definition=self.delegated_tool)
        executor = ToolExecutor.objects.create(
            tenant=self.tenant, name="Executor", executor_type=ToolExecutor.ExecutorType.WORKER, public_id="expired-executor"
        )
        ToolExecutorCapability.objects.create(executor=executor, tool_definition=self.delegated_tool)

        with self.assertRaises(ToolExecutionLifecycleError):
            claim_tool_execution(executor=executor, execution=execution)

        execution.refresh_from_db()
        self.assertEqual(execution.status, ToolExecution.Status.EXPIRED)


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class ToolApiTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username="member", password="pass")
        TenantMembership.objects.create(tenant=self.tenant, user=self.user, role=TenantMembership.Role.VIEWER)
        self.client.force_login(self.user)

    def test_tools_list_requires_auth_and_returns_catalog(self):
        self.client.logout()
        self.assertEqual(self.client.get("/api/v1/tools/").status_code, 401)
        self.client.force_login(self.user)

        response = self.client.get("/api/v1/tools/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["slug"], "prospecting.build_search_plan")

    def test_installation_tools_are_tenant_scoped(self):
        binding = self.bind()
        self.bind(installation=self.other_installation)

        response = self.client.get(f"/api/v1/agent-installations/{self.installation.id}/tools/")
        other_response = self.client.get(f"/api/v1/agent-installations/{self.other_installation.id}/tools/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["id"], str(binding.id))
        self.assertEqual(other_response.status_code, 404)

    def test_execute_binding_runs_tool_and_blocks_cross_tenant(self):
        binding = self.bind(configuration={"max_queries": 2})
        other_binding = self.bind(installation=self.other_installation)

        response = self.client.post(
            f"/api/v1/tool-bindings/{binding.id}/execute/",
            data=json.dumps({"input": {"target_market": "hospitais", "target_region": "São Paulo"}}),
            content_type="application/json",
        )
        other_response = self.client.post(
            f"/api/v1/tool-bindings/{other_binding.id}/execute/",
            data=json.dumps({"input": {}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "planned")
        self.assertEqual(len(response.json()["output"]["queries"]), 2)
        self.assertEqual(other_response.status_code, 404)


    def test_tool_execution_api_claim_complete_and_cross_tenant_scope(self):
        self.bind(tool=self.delegated_tool)
        other_binding = self.bind(installation=self.other_installation, tool=self.delegated_tool)
        execute_tool(installation=self.installation, tool_slug="prospecting.external_search_probe", input={"query": "a"})
        execute_tool(installation=self.other_installation, tool_slug="prospecting.external_search_probe", input={"query": "b"})
        execution = ToolExecution.objects.get(tenant=self.tenant, tool_definition=self.delegated_tool)
        other_execution = ToolExecution.objects.get(tenant=self.other_tenant, tool_definition=self.delegated_tool)
        executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="Test Browser Executor",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="api-browser",
        )
        ToolExecutorCapability.objects.create(executor=executor, tool_definition=self.delegated_tool)

        list_response = self.client.get("/api/v1/tool-executions/")
        detail_other = self.client.get(f"/api/v1/tool-executions/{other_execution.id}/")
        claim = self.client.post(
            f"/api/v1/tool-executions/{execution.id}/claim/",
            data=json.dumps({"executor_public_id": "api-browser"}),
            content_type="application/json",
        )
        complete = self.client.post(
            f"/api/v1/tool-executions/{execution.id}/complete/",
            data=json.dumps({"executor_public_id": "api-browser", "result": {"ok": True}}),
            content_type="application/json",
        )
        claim_other = self.client.post(
            f"/api/v1/tool-executions/{other_execution.id}/claim/",
            data=json.dumps({"executor_public_id": "api-browser"}),
            content_type="application/json",
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual([item["id"] for item in list_response.json()["results"]], [str(execution.id)])
        self.assertEqual(detail_other.status_code, 404)
        self.assertEqual(claim.status_code, 200)
        self.assertEqual(claim.json()["status"], ToolExecution.Status.RUNNING)
        self.assertEqual(complete.status_code, 200)
        self.assertEqual(complete.json()["status"], ToolExecution.Status.SUCCEEDED)
        self.assertEqual(claim_other.status_code, 404)

    def test_tool_execution_api_fail_requires_owner(self):
        self.bind(tool=self.delegated_tool)
        execute_tool(installation=self.installation, tool_slug="prospecting.external_search_probe", input={})
        execution = ToolExecution.objects.get(tool_definition=self.delegated_tool)
        owner = ToolExecutor.objects.create(
            tenant=self.tenant, name="Owner API", executor_type=ToolExecutor.ExecutorType.WORKER, public_id="api-owner"
        )
        other = ToolExecutor.objects.create(
            tenant=self.tenant, name="Other API", executor_type=ToolExecutor.ExecutorType.WORKER, public_id="api-other"
        )
        ToolExecutorCapability.objects.create(executor=owner, tool_definition=self.delegated_tool)
        ToolExecutorCapability.objects.create(executor=other, tool_definition=self.delegated_tool)
        self.client.post(
            f"/api/v1/tool-executions/{execution.id}/claim/",
            data=json.dumps({"executor_public_id": "api-owner"}),
            content_type="application/json",
        )

        wrong = self.client.post(
            f"/api/v1/tool-executions/{execution.id}/fail/",
            data=json.dumps({"executor_public_id": "api-other", "error_code": "wrong"}),
            content_type="application/json",
        )
        fail = self.client.post(
            f"/api/v1/tool-executions/{execution.id}/fail/",
            data=json.dumps({"executor_public_id": "api-owner", "error_code": "boom"}),
            content_type="application/json",
        )

        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(fail.status_code, 200)
        self.assertEqual(fail.json()["status"], ToolExecution.Status.FAILED)
