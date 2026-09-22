import json
from unittest.mock import Mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from audit.models import AuditEvent
from projects.models import Project
from tenants.models import Tenant, TenantMembership
from tools.application.execution import ToolExecutionError, execute_tool
from tools.application.identity import (
    ExecutorAuthenticationError,
    approve_pairing,
    authenticate_executor_credential,
    consume_pairing,
    create_executor_credential,
    reject_pairing,
    request_pairing,
    revoke_credential,
    rotate_credential,
)
from tools.application.lifecycle import (
    ToolExecutionLifecycleError,
    claim_tool_execution,
    complete_tool_execution,
    fail_tool_execution,
    transition_execution,
)
from tools.application.registry import ToolRuntimeRegistry, UnknownToolRuntime
from tools.domain.runtime import ToolResult
from tools.infrastructure.compatibility import is_tool_compatible_with_agent
from tools.infrastructure.google_maps_contract import (
    validate_google_maps_input,
    validate_google_maps_result,
)
from tools.infrastructure.prospecting_build_search_plan import (
    ProspectingBuildSearchPlanTool,
    normalize_build_search_plan_configuration,
)
from tools.models import (
    AgentToolBinding,
    ToolDefinition,
    ToolExecution,
    ToolExecutor,
    ToolExecutorCapability,
    ToolExecutorCredential,
    ToolExecutorPairingRequest,
)


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
        self.google_maps_tool = ToolDefinition.objects.get(slug="prospecting.search_google_maps")

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

    def test_google_maps_tool_contract_is_bootstrapped_without_runtime_or_auto_grants(self):
        self.assertEqual(self.google_maps_tool.name, "Search Google Maps")
        self.assertEqual(self.google_maps_tool.category, "prospecting")
        self.assertEqual(self.google_maps_tool.execution_mode, ToolDefinition.ExecutionMode.DELEGATED)
        self.assertEqual(self.google_maps_tool.runtime_handler, "")
        self.assertTrue(self.google_maps_tool.is_active)
        self.assertFalse(AgentToolBinding.objects.filter(tool_definition=self.google_maps_tool).exists())
        self.assertFalse(ToolExecutorCapability.objects.filter(tool_definition=self.google_maps_tool).exists())

    def test_google_maps_tool_is_compatible_only_with_prospecting_agent(self):
        self.assertTrue(is_tool_compatible_with_agent(tool_slug=self.google_maps_tool.slug, agent_slug="prospecting"))
        self.assertFalse(is_tool_compatible_with_agent(tool_slug=self.google_maps_tool.slug, agent_slug="livia"))

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

    def test_tool_execution_result_allows_redacted_secret_markers(self):
        binding = self.bind(tool=self.delegated_tool)
        execution = ToolExecution(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            tool_binding=binding,
            tool_definition=self.delegated_tool,
            execution_mode=ToolDefinition.ExecutionMode.DELEGATED,
            result_payload={"received": {"api_key": "[redacted]"}},
        )

        execution.full_clean()

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


class GoogleMapsContractTests(ToolTestCase):
    def valid_input(self):
        return {
            "schema_version": 1,
            "queries": [" hospital privado  São Paulo "],
            "target_region": " São Paulo ",
            "max_results": 50,
        }

    def valid_result(self):
        return {
            "schema_version": 1,
            "status": " completed ",
            "businesses": [
                {
                    "name": " Hospital A ",
                    "category": "Hospital",
                    "address": "Rua A, 1",
                    "phone": None,
                    "website": "HTTPS://EXAMPLE.COM/",
                    "maps_url": "https://maps.google.com/?cid=1",
                    "external_id": "cid:1",
                    "source_query": "hospital privado São Paulo",
                }
            ],
            "stats": {
                "queries_requested": 1,
                "queries_executed": 1,
                "businesses_found": 1,
                "businesses_returned": 1,
                "duplicates_removed": 0,
                "duration_ms": 1200,
            },
        }

    def execution_for_maps(self, max_results=50):
        binding, _ = AgentToolBinding.objects.get_or_create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            tool_definition=self.google_maps_tool,
            defaults={"is_enabled": True, "configuration": {}},
        )
        return ToolExecution.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            tool_binding=binding,
            tool_definition=self.google_maps_tool,
            execution_mode=ToolDefinition.ExecutionMode.DELEGATED,
            status=ToolExecution.Status.RUNNING,
            request_payload={
                "input": {
                    "schema_version": 1,
                    "queries": ["hospital privado São Paulo"],
                    "target_region": "São Paulo",
                    "max_results": max_results,
                    "locale": "pt-BR",
                }
            },
        )

    def test_input_contract_normalizes_valid_payload(self):
        normalized = validate_google_maps_input(self.valid_input())

        self.assertEqual(normalized["queries"], ["hospital privado São Paulo"])
        self.assertEqual(normalized["target_region"], "São Paulo")
        self.assertEqual(normalized["locale"], "pt-BR")
        self.assertEqual(normalized["max_results"], 50)

    def test_input_contract_rejects_invalid_shapes_and_urls(self):
        invalid_payloads = [
            {},
            {**self.valid_input(), "schema_version": 2},
            {**self.valid_input(), "queries": []},
            {**self.valid_input(), "queries": ["x"] * 21},
            {**self.valid_input(), "queries": ["https://maps.google.com/search?q=x"]},
            {**self.valid_input(), "max_results": 0},
            {**self.valid_input(), "max_results": 101},
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    validate_google_maps_input(payload)

    def test_input_secret_safety_blocks_plaintext_and_allows_redacted(self):
        with self.assertRaises(ValidationError):
            validate_google_maps_input({**self.valid_input(), "cookie": "session=value"})

        payload = validate_google_maps_input({**self.valid_input(), "credential": "[redacted]"})
        self.assertEqual(payload["locale"], "pt-BR")

    def test_output_contract_accepts_optional_business_fields(self):
        result = self.valid_result()
        result["businesses"][0].update({"category": None, "address": None, "phone": None, "website": None})

        normalized = validate_google_maps_result(result, self.execution_for_maps())

        self.assertEqual(normalized["status"], "completed")
        self.assertEqual(normalized["businesses"][0]["name"], "Hospital A")
        self.assertEqual(normalized["businesses"][0]["category"], None)

    def test_output_contract_rejects_missing_identifier_too_many_results_and_secrets(self):
        result = self.valid_result()
        result["businesses"][0]["maps_url"] = None
        result["businesses"][0]["external_id"] = None
        with self.assertRaises(ValidationError):
            validate_google_maps_result(result, self.execution_for_maps())

        too_many = self.valid_result()
        too_many["businesses"] = [self.valid_result()["businesses"][0]] * 2
        with self.assertRaises(ValidationError):
            validate_google_maps_result(too_many, self.execution_for_maps(max_results=1))

        with self.assertRaises(ValidationError):
            validate_google_maps_result({**self.valid_result(), "authorization": "Bearer secret"}, self.execution_for_maps())

    def test_output_contract_dedupes_by_external_id_then_maps_url(self):
        first = self.valid_result()["businesses"][0]
        duplicate_external_id = {**first, "name": "Hospital A Unidade 2", "maps_url": "https://maps.google.com/?cid=2"}
        duplicate_url = {**first, "external_id": None, "maps_url": "https://maps.google.com/?cid=3"}
        duplicate_url_again = {**first, "external_id": None, "maps_url": "HTTPS://MAPS.GOOGLE.COM/?cid=3"}
        result = {**self.valid_result(), "businesses": [first, duplicate_external_id, duplicate_url, duplicate_url_again]}

        normalized = validate_google_maps_result(result, self.execution_for_maps())

        self.assertEqual(len(normalized["businesses"]), 2)
        self.assertEqual(normalized["stats"]["duplicates_removed"], 2)


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

    def test_google_maps_execute_validates_input_and_dispatches(self):
        self.bind(tool=self.google_maps_tool, configuration={})

        result = execute_tool(
            installation=self.installation,
            tool_slug="prospecting.search_google_maps",
            input={
                "schema_version": 1,
                "queries": [" hospital privado São Paulo "],
                "target_region": "São Paulo",
                "max_results": 25,
            },
        )

        execution = ToolExecution.objects.get(tool_definition=self.google_maps_tool)
        self.assertEqual(result.status, "dispatched")
        self.assertEqual(execution.status, ToolExecution.Status.DISPATCHED)
        self.assertEqual(execution.request_payload["input"]["queries"], ["hospital privado São Paulo"])
        self.assertEqual(execution.request_payload["input"]["locale"], "pt-BR")

    def test_google_maps_invalid_input_does_not_create_execution(self):
        self.bind(tool=self.google_maps_tool, configuration={})

        with self.assertRaises(ValidationError):
            execute_tool(
                installation=self.installation,
                tool_slug="prospecting.search_google_maps",
                input={"schema_version": 1, "queries": [], "max_results": 10},
            )

        self.assertFalse(ToolExecution.objects.filter(tool_definition=self.google_maps_tool).exists())

    def test_google_maps_idempotency_reuses_delegated_execution(self):
        self.bind(tool=self.google_maps_tool, configuration={})
        payload = {
            "schema_version": 1,
            "queries": ["hospital privado São Paulo"],
            "max_results": 10,
        }

        first = execute_tool(
            installation=self.installation,
            tool_slug="prospecting.search_google_maps",
            input=payload,
            idempotency_key="maps-run",
        )
        second = execute_tool(
            installation=self.installation,
            tool_slug="prospecting.search_google_maps",
            input=payload,
            idempotency_key="maps-run",
        )

        self.assertEqual(first.status, "dispatched")
        self.assertEqual(second.status, "dispatched")
        self.assertEqual(ToolExecution.objects.filter(tool_definition=self.google_maps_tool, idempotency_key="maps-run").count(), 1)

    def test_google_maps_complete_validates_result_before_success(self):
        self.bind(tool=self.google_maps_tool, configuration={})
        execute_tool(
            installation=self.installation,
            tool_slug="prospecting.search_google_maps",
            input={"schema_version": 1, "queries": ["hospital privado São Paulo"], "max_results": 1},
        )
        execution = ToolExecution.objects.get(tool_definition=self.google_maps_tool)
        executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="Maps Browser Executor",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="maps-browser-a",
        )
        ToolExecutorCapability.objects.create(executor=executor, tool_definition=self.google_maps_tool)
        claimed = claim_tool_execution(executor=executor, execution=execution)
        completed = complete_tool_execution(
            executor=executor,
            execution=claimed,
            result={
                "schema_version": 1,
                "status": "completed",
                "businesses": [
                    {
                        "name": "Hospital A",
                        "category": None,
                        "address": "Rua A, 1",
                        "phone": None,
                        "website": None,
                        "maps_url": "https://maps.google.com/?cid=1",
                        "external_id": None,
                        "source_query": "hospital privado São Paulo",
                    }
                ],
                "stats": {"duration_ms": 100, "queries_requested": 1, "queries_executed": 1, "businesses_found": 1, "businesses_returned": 1, "duplicates_removed": 0},
            },
        )

        self.assertEqual(completed.status, ToolExecution.Status.SUCCEEDED)
        self.assertEqual(completed.result_payload["businesses"][0]["name"], "Hospital A")

    def test_google_maps_invalid_result_marks_failed_without_payload(self):
        self.bind(tool=self.google_maps_tool, configuration={})
        execute_tool(
            installation=self.installation,
            tool_slug="prospecting.search_google_maps",
            input={"schema_version": 1, "queries": ["hospital privado São Paulo"], "max_results": 1},
        )
        execution = ToolExecution.objects.get(tool_definition=self.google_maps_tool)
        executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="Maps Browser Executor Invalid",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="maps-browser-invalid",
        )
        ToolExecutorCapability.objects.create(executor=executor, tool_definition=self.google_maps_tool)
        claimed = claim_tool_execution(executor=executor, execution=execution)

        completed = complete_tool_execution(
            executor=executor,
            execution=claimed,
            result={"schema_version": 1, "status": "completed", "businesses": [{"name": "No ID"}], "stats": {}},
        )

        self.assertEqual(completed.status, ToolExecution.Status.FAILED)
        self.assertEqual(completed.error_code, "invalid_tool_result")
        self.assertEqual(completed.result_payload, {})
        self.assertTrue(AuditEvent.objects.filter(action="tool.execution.failed", object_id=str(execution.pk)).exists())

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


class ExecutorCredentialAndPairingTests(ToolTestCase):
    def test_pairing_lifecycle_consumes_credential_once_without_plaintext_storage(self):
        user = get_user_model().objects.create_user(username="admin", password="pass")
        pairing = request_pairing(
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            requested_name="Chrome Ext",
        )

        self.assertIsNone(pairing.tenant_id)
        approved = approve_pairing(pairing=pairing, tenant=self.tenant, actor=user)
        issued = consume_pairing(pairing_id=approved.id, pairing_code=approved.pairing_code)

        approved.refresh_from_db()
        self.assertEqual(approved.status, ToolExecutorPairingRequest.Status.CONSUMED)
        self.assertTrue(issued.secret.startswith(f"aep_{issued.credential.credential_prefix}_"))
        self.assertNotEqual(issued.credential.secret_hash, issued.secret)
        self.assertNotIn(issued.secret, issued.credential.secret_hash)
        with self.assertRaises(ValidationError):
            consume_pairing(pairing_id=approved.id, pairing_code=approved.pairing_code)

    def test_pairing_expired_rejected_and_invalid_code_are_blocked(self):
        expired = request_pairing(executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION, requested_name="Old")
        expired.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        expired.save(update_fields=["expires_at"])
        with self.assertRaises(ValidationError):
            approve_pairing(pairing=expired, tenant=self.tenant)
        expired.refresh_from_db()
        self.assertEqual(expired.status, ToolExecutorPairingRequest.Status.EXPIRED)

        rejected = request_pairing(executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION, requested_name="No")
        reject_pairing(pairing=rejected)
        rejected.refresh_from_db()
        self.assertEqual(rejected.status, ToolExecutorPairingRequest.Status.REJECTED)
        with self.assertRaises(ValidationError):
            approve_pairing(pairing=rejected, tenant=self.tenant)

        approved = approve_pairing(
            pairing=request_pairing(executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION, requested_name="Bad code"),
            tenant=self.tenant,
        )
        with self.assertRaises(ValidationError):
            consume_pairing(pairing_id=approved.id, pairing_code="WRONG")

    def test_executor_credential_authentication_rotation_revocation_and_expiration(self):
        executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="Auth Executor",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="auth-executor",
        )
        issued = create_executor_credential(executor=executor)

        principal = authenticate_executor_credential(f"AgentExecutor {issued.secret}")
        self.assertEqual(principal.executor_id, str(executor.id))
        issued.credential.refresh_from_db()
        self.assertIsNotNone(issued.credential.last_used_at)

        with self.assertRaises(ExecutorAuthenticationError):
            authenticate_executor_credential(f"AgentExecutor aep_{issued.credential.credential_prefix}_wrong")

        rotated = rotate_credential(credential=issued.credential)
        with self.assertRaises(ExecutorAuthenticationError):
            authenticate_executor_credential(f"AgentExecutor {issued.secret}")
        self.assertEqual(authenticate_executor_credential(f"Bearer {rotated.secret}").credential_id, str(rotated.credential.id))

        revoke_credential(credential=rotated.credential)
        with self.assertRaises(ExecutorAuthenticationError):
            authenticate_executor_credential(f"AgentExecutor {rotated.secret}")

        expired = create_executor_credential(executor=executor, expires_at=timezone.now() - timezone.timedelta(seconds=1))
        with self.assertRaises(ExecutorAuthenticationError):
            authenticate_executor_credential(f"AgentExecutor {expired.secret}")

    def test_inactive_executor_blocks_valid_credential(self):
        executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="Inactive Executor",
            executor_type=ToolExecutor.ExecutorType.WORKER,
            public_id="inactive-executor",
            is_active=False,
        )
        issued = create_executor_credential(executor=executor)

        with self.assertRaises(ExecutorAuthenticationError):
            authenticate_executor_credential(f"AgentExecutor {issued.secret}")


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class ExecutorApiTests(ToolTestCase):
    def test_executor_pairing_api_accepts_extension_post_without_csrf_cookie(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            "/api/v1/executors/pairing/request/",
            data=json.dumps({"requested_name": "Extension", "executor_type": "BROWSER_EXTENSION"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)

    def test_executor_mutations_ignore_human_session_without_executor_credential(self):
        user = get_user_model().objects.create_user(username="executor-session-user", password="pass")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)

        response = client.post("/api/v1/executors/heartbeat/")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "executor_authentication_failed")

    def _executor_with_secret(self, tenant=None, public_id="executor-api", active=True, capability=True):
        executor = ToolExecutor.objects.create(
            tenant=tenant or self.tenant,
            name=public_id,
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id=public_id,
            is_active=active,
        )
        if capability:
            ToolExecutorCapability.objects.create(executor=executor, tool_definition=self.delegated_tool)
        issued = create_executor_credential(executor=executor)
        return executor, issued.secret

    def test_pairing_api_request_status_consume_me_and_heartbeat(self):
        response = self.client.post(
            "/api/v1/executors/pairing/request/",
            data=json.dumps({"requested_name": "Extension", "executor_type": "BROWSER_EXTENSION"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        pairing = ToolExecutorPairingRequest.objects.get(pk=response.json()["id"])
        approved_pairing = approve_pairing(pairing=pairing, tenant=self.tenant)
        ToolExecutorCapability.objects.create(executor=approved_pairing.executor, tool_definition=self.delegated_tool)

        status_response = self.client.get(f"/api/v1/executors/pairing/{pairing.id}/status/")
        consume_response = self.client.post(
            f"/api/v1/executors/pairing/{pairing.id}/consume/",
            data=json.dumps({"pairing_code": pairing.pairing_code}),
            content_type="application/json",
        )
        secret = consume_response.json()["credential"]
        second_consume = self.client.post(
            f"/api/v1/executors/pairing/{pairing.id}/consume/",
            data=json.dumps({"pairing_code": pairing.pairing_code}),
            content_type="application/json",
        )
        me = self.client.get("/api/v1/executors/me/", HTTP_AUTHORIZATION=f"AgentExecutor {secret}")
        heartbeat = self.client.post("/api/v1/executors/heartbeat/", HTTP_AUTHORIZATION=f"AgentExecutor {secret}")

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(consume_response.status_code, 200)
        self.assertEqual(second_consume.status_code, 400)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(
            consume_response.json()["executor"]["capabilities"][0]["slug"], "prospecting.external_search_probe"
        )
        self.assertEqual(me.json()["executor"]["capabilities"][0]["slug"], "prospecting.external_search_probe")
        self.assertEqual(
            heartbeat.json()["executor"]["capabilities"][0]["slug"], "prospecting.external_search_probe"
        )
        self.assertIsNotNone(ToolExecutor.objects.get(pk=me.json()["executor"]["id"]).last_seen_at)

    def test_executor_queue_filters_by_tenant_capability_and_status(self):
        self.bind(tool=self.delegated_tool)
        self.bind(installation=self.other_installation, tool=self.delegated_tool)
        execute_tool(installation=self.installation, tool_slug="prospecting.external_search_probe", input={"query": "a"})
        execute_tool(installation=self.other_installation, tool_slug="prospecting.external_search_probe", input={"query": "b"})
        local_binding = self.bind(tool=self.tool)
        ToolExecution.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            tool_binding=local_binding,
            tool_definition=self.tool,
            execution_mode=ToolDefinition.ExecutionMode.LOCAL,
            status=ToolExecution.Status.DISPATCHED,
        )
        _, secret = self._executor_with_secret()

        response = self.client.get("/api/v1/executors/tool-executions/", HTTP_AUTHORIZATION=f"AgentExecutor {secret}")

        self.assertEqual(response.status_code, 200)
        ids = [item["id"] for item in response.json()["results"]]
        expected = ToolExecution.objects.get(tenant=self.tenant, tool_definition=self.delegated_tool)
        self.assertEqual(ids, [str(expected.id)])

    def test_executor_claim_complete_fail_security(self):
        self.bind(tool=self.delegated_tool)
        execute_tool(installation=self.installation, tool_slug="prospecting.external_search_probe", input={})
        execution = ToolExecution.objects.get(tool_definition=self.delegated_tool)
        owner, owner_secret = self._executor_with_secret(public_id="owner-auth")
        _, other_secret = self._executor_with_secret(public_id="other-auth")

        claim = self.client.post(
            f"/api/v1/executors/tool-executions/{execution.id}/claim/",
            HTTP_AUTHORIZATION=f"AgentExecutor {owner_secret}",
        )
        wrong_complete = self.client.post(
            f"/api/v1/executors/tool-executions/{execution.id}/complete/",
            data=json.dumps({"result": {"bad": True}}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"AgentExecutor {other_secret}",
        )
        complete = self.client.post(
            f"/api/v1/executors/tool-executions/{execution.id}/complete/",
            data=json.dumps({"result": {"ok": True}}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"AgentExecutor {owner_secret}",
        )

        self.assertEqual(claim.status_code, 200)
        self.assertEqual(wrong_complete.status_code, 400)
        self.assertEqual(complete.status_code, 200)
        self.assertEqual(complete.json()["status"], ToolExecution.Status.SUCCEEDED)
        self.assertEqual(ToolExecution.objects.get(pk=execution.pk).executor_id, owner.id)

    def test_executor_without_capability_inactive_and_invalid_credential_are_blocked(self):
        self.bind(tool=self.delegated_tool)
        execute_tool(installation=self.installation, tool_slug="prospecting.external_search_probe", input={})
        execution = ToolExecution.objects.get(tool_definition=self.delegated_tool)
        _, no_cap_secret = self._executor_with_secret(public_id="no-cap", capability=False)
        _, inactive_secret = self._executor_with_secret(public_id="inactive-api", active=False)

        no_cap = self.client.post(
            f"/api/v1/executors/tool-executions/{execution.id}/claim/",
            HTTP_AUTHORIZATION=f"AgentExecutor {no_cap_secret}",
        )
        inactive = self.client.get("/api/v1/executors/me/", HTTP_AUTHORIZATION=f"AgentExecutor {inactive_secret}")
        invalid = self.client.get("/api/v1/executors/me/", HTTP_AUTHORIZATION="AgentExecutor aep_missing_bad")

        self.assertEqual(no_cap.status_code, 400)
        self.assertEqual(inactive.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertTrue(AuditEvent.objects.filter(action="executor.auth.failed").exists())
