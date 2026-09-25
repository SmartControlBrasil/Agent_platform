from __future__ import annotations

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from io import StringIO

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from prospecting.application.enrichments import add_prospect_enrichment
from prospecting.application.prospects import promote_search_result_to_prospect
from prospecting.application.contacts import (
    ACTION_PROSPECT_CONTACT_CREATED,
    ACTION_PROSPECT_CONTACT_DELETED,
    ACTION_PROSPECT_CONTACT_UPDATED,
    create_prospect_contact,
    delete_prospect_contact,
    update_prospect_contact,
)
from prospecting.application.qualification import ACTION_PROSPECT_QUALIFICATION_UPDATED, qualify_prospect
from prospecting.application.review import (
    ProspectingReviewError,
    bulk_ignore_search_results,
    bulk_promote_search_results,
    mark_search_result_ignored,
    restore_search_result_to_unreviewed,
)
from prospecting.application.execution_recovery import (
    ProspectingExecutionRecoveryError,
    cancel_search_run,
    retry_search_run,
)
from prospecting.application.search_runs import (
    build_search_plan_for_manual_run,
    build_search_run_attempt_idempotency_key,
    create_and_dispatch_search_run,
    synchronize_search_run,
)
from prospecting.application.website_enrichment import enrich_prospect_from_website
from prospecting.infrastructure.website_fetcher import validate_public_http_url
from prospecting.interfaces.website import WebsiteFetchResult
from prospecting.models import Prospect, ProspectContact, ProspectEnrichment, ProspectSource, SearchResult, SearchRun, SearchRunExecutionAttempt
from tenants.models import Tenant
from audit.models import AuditEvent
from django.contrib.auth import get_user_model
from tools.application.lifecycle import claim_tool_execution, complete_tool_execution, fail_tool_execution
from tools.models import (
    AgentToolBinding,
    ToolDefinition,
    ToolExecution,
    ToolExecutor,
    ToolExecutorCapability,
    ToolExecutorCredential,
)


class ProspectingDomainTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant")
        self.project = Project.objects.create(tenant=self.tenant, name="Projeto A", slug="project-a")
        self.other_project = Project.objects.create(tenant=self.other_tenant, name="Projeto B", slug="project-b")

        self.definition = AgentDefinition.objects.get(slug="prospecting")
        self.version = AgentVersion.objects.get(agent_definition=self.definition, version="1.0.0")
        self.installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Prospecting A",
        )

        self.search_run = SearchRun.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            target_market="hospitais",
            target_region="São Paulo",
            target_profile="privados",
            objective="buscar contas",
            queries=["hospital privado são paulo"],
            max_results=10,
        )
        self.search_result = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=self.search_run,
            name="Hospital Exemplo",
            address="Rua A, 10",
            phone="(11) 99999-1111",
            website="https://hospital.example.com",
            maps_url="https://maps.google.com/?cid=1",
            external_id="cid:1",
            source_query="hospital privado são paulo",
        )

    def test_search_run_requires_prospecting_installation(self):
        livia_definition, _ = AgentDefinition.objects.update_or_create(
            slug="livia",
            defaults={"name": "Lívia", "agent_type": "conversational_sales", "is_active": True},
        )
        livia_version, _ = AgentVersion.objects.update_or_create(
            agent_definition=livia_definition,
            version="legacy-initial",
            defaults={"runtime_handler": "livia", "status": AgentVersion.Status.ACTIVE},
        )
        livia_installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=livia_definition,
            agent_version=livia_version,
            name="Lívia",
        )
        run = SearchRun(
            tenant=self.tenant,
            project=self.project,
            agent_installation=livia_installation,
            target_region="São Paulo",
            queries=["hospital privado são paulo"],
            max_results=10,
        )
        with self.assertRaises(ValidationError):
            run.full_clean()

    def test_promote_search_result_creates_prospect_and_provenance(self):
        prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)

        self.assertEqual(Prospect.objects.count(), 1)
        self.assertEqual(ProspectSource.objects.count(), 1)
        source = ProspectSource.objects.get(search_result=self.search_result)
        self.assertEqual(source.prospect_id, prospect.id)
        self.assertEqual(source.search_run_id, self.search_run.id)
        self.assertEqual(prospect.qualification_status, Prospect.QualificationStatus.UNQUALIFIED)
        self.assertEqual(prospect.priority, Prospect.Priority.UNSET)

    def test_promotion_is_idempotent(self):
        first = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)
        second = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)

        self.assertEqual(first.id, second.id)
        self.assertEqual(Prospect.objects.count(), 1)
        self.assertEqual(ProspectSource.objects.count(), 1)

    def test_dedupe_prefers_external_identity(self):
        run = SearchRun.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            target_market="hospitais",
            target_region="Campinas",
            objective="buscar contas",
            queries=["hospital campinas"],
            max_results=10,
        )
        result_b = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=run,
            name="Hospital Exemplo Filial",
            external_id="cid:1",
        )

        first = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)
        second = promote_search_result_to_prospect(tenant=self.tenant, search_result=result_b)

        self.assertEqual(first.id, second.id)
        self.assertEqual(ProspectSource.objects.count(), 2)

    def test_never_dedupes_by_name_only(self):
        run = SearchRun.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            target_market="hospitais",
            target_region="Rio de Janeiro",
            objective="buscar contas",
            queries=["hospital rio"],
            max_results=10,
        )
        result_b = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=run,
            name="Hospital Exemplo",
            address="",
            external_id="",
            maps_url="",
        )

        first = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)
        second = promote_search_result_to_prospect(tenant=self.tenant, search_result=result_b)

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(Prospect.objects.count(), 2)

    def test_cross_tenant_promotion_is_blocked(self):
        with self.assertRaises(ValidationError):
            promote_search_result_to_prospect(tenant=self.other_tenant, search_result=self.search_result)

    def test_search_result_and_search_run_remain_intact_after_promotion(self):
        before_run_status = self.search_run.status
        before_result_name = self.search_result.name

        promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)
        self.search_run.refresh_from_db()
        self.search_result.refresh_from_db()

        self.assertEqual(self.search_run.status, before_run_status)
        self.assertEqual(self.search_result.name, before_result_name)

    def test_search_result_starts_unreviewed(self):
        self.assertEqual(self.search_result.review_status, SearchResult.ReviewStatus.UNREVIEWED)

    def test_ignore_result_is_idempotent_and_reversible(self):
        mark_search_result_ignored(tenant=self.tenant, search_result=self.search_result)
        self.search_result.refresh_from_db()
        self.assertEqual(self.search_result.review_status, SearchResult.ReviewStatus.IGNORED)

        mark_search_result_ignored(tenant=self.tenant, search_result=self.search_result)
        self.search_result.refresh_from_db()
        self.assertEqual(self.search_result.review_status, SearchResult.ReviewStatus.IGNORED)

        restore_search_result_to_unreviewed(tenant=self.tenant, search_result=self.search_result)
        self.search_result.refresh_from_db()
        self.assertEqual(self.search_result.review_status, SearchResult.ReviewStatus.UNREVIEWED)

    def test_ignore_keeps_search_run_and_result_persisted(self):
        run_id = self.search_run.id
        result_id = self.search_result.id
        mark_search_result_ignored(tenant=self.tenant, search_result=self.search_result)
        self.assertTrue(SearchRun.objects.filter(id=run_id).exists())
        self.assertTrue(SearchResult.objects.filter(id=result_id).exists())

    def test_cross_tenant_ignore_is_blocked(self):
        with self.assertRaises(ProspectingReviewError):
            mark_search_result_ignored(tenant=self.other_tenant, search_result=self.search_result)

    def test_promoted_result_cannot_be_ignored(self):
        promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)
        with self.assertRaises(ProspectingReviewError):
            mark_search_result_ignored(tenant=self.tenant, search_result=self.search_result)

    def test_bulk_ignore_summary_counts_promoted_conflict(self):
        promoted_result = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=self.search_run,
            name="Hospital Promovido",
            external_id="cid:promoted",
        )
        promote_search_result_to_prospect(tenant=self.tenant, search_result=promoted_result)
        summary = bulk_ignore_search_results(tenant=self.tenant, search_results=[self.search_result, promoted_result])
        self.search_result.refresh_from_db()
        self.assertEqual(self.search_result.review_status, SearchResult.ReviewStatus.IGNORED)
        self.assertEqual(summary.ignored, 1)
        self.assertEqual(summary.promoted_conflict, 1)

    def test_bulk_promote_remains_idempotent_and_keeps_provenance(self):
        second_result = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=self.search_run,
            name="Hospital B",
            external_id="cid:2",
            review_status=SearchResult.ReviewStatus.IGNORED,
        )
        first_summary = bulk_promote_search_results(tenant=self.tenant, search_results=[self.search_result, second_result])
        second_summary = bulk_promote_search_results(tenant=self.tenant, search_results=[self.search_result, second_result])
        second_result.refresh_from_db()

        self.assertEqual(first_summary.promoted, 2)
        self.assertEqual(second_summary.already_promoted, 2)
        self.assertEqual(ProspectSource.objects.filter(search_result=self.search_result).count(), 1)
        self.assertEqual(ProspectSource.objects.filter(search_result=second_result).count(), 1)
        self.assertEqual(second_result.review_status, SearchResult.ReviewStatus.UNREVIEWED)

    def test_manual_enrichment_normalizes_and_is_idempotent(self):
        prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)
        first = add_prospect_enrichment(
            tenant=self.tenant,
            prospect=prospect,
            field=ProspectEnrichment.Field.EMAIL,
            value=" COMERCIAL@HOSPITAL.EXAMPLE.COM ",
            source_type=ProspectEnrichment.SourceType.MANUAL,
            source_reference="planilha",
        )
        second = add_prospect_enrichment(
            tenant=self.tenant,
            prospect=prospect,
            field=ProspectEnrichment.Field.EMAIL,
            value="comercial@hospital.example.com",
            source_type=ProspectEnrichment.SourceType.MANUAL,
            source_reference="planilha",
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.normalized_value, "comercial@hospital.example.com")
        self.assertEqual(ProspectEnrichment.objects.count(), 1)

    def test_enrichment_keeps_multiple_values_and_does_not_overwrite_prospect(self):
        prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)
        original_phone = prospect.phone
        add_prospect_enrichment(
            tenant=self.tenant,
            prospect=prospect,
            field=ProspectEnrichment.Field.PHONE,
            value="+55 11 98888-1000",
            source_type=ProspectEnrichment.SourceType.WEBSITE,
            source_url="https://hospital.example.com/contato",
        )
        add_prospect_enrichment(
            tenant=self.tenant,
            prospect=prospect,
            field=ProspectEnrichment.Field.PHONE,
            value="+55 11 97777-1000",
            source_type=ProspectEnrichment.SourceType.WEBSITE,
            source_url="https://hospital.example.com/fale-conosco",
        )

        prospect.refresh_from_db()
        self.assertEqual(prospect.phone, original_phone)
        self.assertEqual(ProspectEnrichment.objects.filter(prospect=prospect, field=ProspectEnrichment.Field.PHONE).count(), 2)

    def test_enrichment_preserves_search_result_provenance(self):
        prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)
        enrichment = add_prospect_enrichment(
            tenant=self.tenant,
            prospect=prospect,
            field=ProspectEnrichment.Field.WEBSITE,
            value="https://hospital.example.com/contato",
            source_type=ProspectEnrichment.SourceType.SEARCH_RESULT,
            source_search_result=self.search_result,
        )
        self.assertEqual(enrichment.source_search_result_id, self.search_result.id)
        self.assertIn("search-result", enrichment.source_key)

    @override_settings(
        WEBSITE_ENRICHMENT_MAX_PAGES=3,
        WEBSITE_ENRICHMENT_CONNECT_TIMEOUT=1,
        WEBSITE_ENRICHMENT_READ_TIMEOUT=1,
        WEBSITE_ENRICHMENT_MAX_REDIRECTS=2,
        WEBSITE_ENRICHMENT_MAX_BODY_BYTES=200000,
        WEBSITE_ENRICHMENT_USER_AGENT="AgentPlatformWebsiteEnrichment/1.0",
    )
    def test_website_enrichment_collects_contacts_with_fake_fetcher(self):
        prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)

        class FakeFetcher:
            def __init__(self):
                self.calls = []

            def fetch(self, url):
                self.calls.append(url)
                if url.endswith("/contato"):
                    return WebsiteFetchResult(
                        url=url,
                        status_code=200,
                        content_type="text/html",
                        body="Telefone +55 11 97777-1234 email vendas@hospital.example.com",
                    )
                return WebsiteFetchResult(
                    url=url,
                    status_code=200,
                    content_type="text/html",
                    body='<a href="/contato">Contato</a> E-mail contato@hospital.example.com',
                )

        result = enrich_prospect_from_website(tenant=self.tenant, prospect=prospect, fetcher=FakeFetcher())

        self.assertGreaterEqual(result.pages_fetched, 1)
        self.assertGreaterEqual(result.enrichments_created, 2)
        self.assertTrue(
            ProspectEnrichment.objects.filter(
                prospect=prospect,
                field=ProspectEnrichment.Field.EMAIL,
                normalized_value="contato@hospital.example.com",
            ).exists()
        )
        self.assertTrue(
            ProspectEnrichment.objects.filter(
                prospect=prospect,
                field=ProspectEnrichment.Field.DOMAIN,
            ).exists()
        )

    def test_website_url_validation_blocks_localhost_and_private_ip(self):
        with patch("prospecting.infrastructure.website_fetcher.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ValidationError):
                validate_public_http_url("https://localhost")
        with patch("prospecting.infrastructure.website_fetcher.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(ValidationError):
                validate_public_http_url("https://interna.example")

    @override_settings(
        WEBSITE_ENRICHMENT_MAX_PAGES=3,
        WEBSITE_ENRICHMENT_CONNECT_TIMEOUT=1,
        WEBSITE_ENRICHMENT_READ_TIMEOUT=1,
        WEBSITE_ENRICHMENT_MAX_REDIRECTS=2,
        WEBSITE_ENRICHMENT_MAX_BODY_BYTES=200000,
        WEBSITE_ENRICHMENT_USER_AGENT="AgentPlatformWebsiteEnrichment/1.0",
    )
    def test_website_enrichment_keeps_partial_result_when_secondary_fetch_fails(self):
        prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)

        class PartialFetcher:
            def fetch(self, url):
                if url.endswith("/contato"):
                    raise ValidationError("timeout ao acessar /contato")
                return WebsiteFetchResult(
                    url=url,
                    status_code=200,
                    content_type="text/html",
                    body='<a href="/contato">Contato</a> contato@hospital.example.com',
                )

        result = enrich_prospect_from_website(tenant=self.tenant, prospect=prospect, fetcher=PartialFetcher())
        self.assertGreaterEqual(result.pages_fetched, 1)
        self.assertGreaterEqual(result.enrichments_created, 1)
        self.assertTrue(result.warnings)


class ProspectingSearchRunExecutionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        self.project = Project.objects.create(tenant=self.tenant, name="Projeto A", slug="project-a")
        self.definition = AgentDefinition.objects.get(slug="prospecting")
        self.version = AgentVersion.objects.get(agent_definition=self.definition, version="1.0.0")
        self.installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Prospecting A",
            configuration={
                "target_market": "hospitais",
                "target_profile": "hospitais privados",
                "max_results": 5,
            },
        )
        self.plan_tool = ToolDefinition.objects.get(slug="prospecting.build_search_plan")
        self.maps_tool = ToolDefinition.objects.get(slug="prospecting.search_google_maps")
        AgentToolBinding.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            tool_definition=self.plan_tool,
            configuration={"max_queries": 4},
        )
        AgentToolBinding.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            tool_definition=self.maps_tool,
            configuration={},
        )

    def test_build_search_plan_uses_bound_tool_path(self):
        plan = build_search_plan_for_manual_run(
            tenant=self.tenant,
            project=self.project,
            objective="Encontrar hospitais para robótica",
            target_region="Barueri",
        )

        self.assertEqual(len(plan["queries"]), 4)
        self.assertEqual(plan["installation"].id, self.installation.id)
        self.assertEqual(
            ToolExecution.objects.filter(
                agent_installation=self.installation,
                tool_definition=self.plan_tool,
                status=ToolExecution.Status.SUCCEEDED,
            ).count(),
            1,
        )

    def test_create_run_dispatches_google_maps_with_single_execution(self):
        run = create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Encontrar hospitais para robótica",
            target_region="Barueri",
            selected_queries=["hospitais Barueri", "hospitais privados Barueri"],
        )

        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        self.assertEqual(run.status, SearchRun.Status.DISPATCHED)
        self.assertEqual(run.max_results, 5)
        self.assertEqual(run.queries, ["hospitais Barueri", "hospitais privados Barueri"])
        self.assertEqual(execution.tool_definition.slug, "prospecting.search_google_maps")
        self.assertEqual(execution.request_payload["input"]["queries"], run.queries)
        self.assertEqual(execution.request_payload["input"]["target_region"], "Barueri")
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=run).count(), 1)
        self.assertEqual(execution.idempotency_key, build_search_run_attempt_idempotency_key(run=run, attempt_number=1))

    def test_sync_succeeded_materializes_results_without_duplicates(self):
        run = create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Encontrar hospitais para robótica",
            target_region="Barueri",
            selected_queries=["hospitais Barueri"],
        )
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="Executor Browser",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="browser-a",
        )
        ToolExecutorCapability.objects.create(executor=executor, tool_definition=self.maps_tool)
        claim_tool_execution(executor=executor, execution=execution)
        complete_tool_execution(
            executor=executor,
            execution=execution,
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
                        "external_id": "cid:1",
                        "source_query": "hospitais Barueri",
                    }
                ],
                "stats": {
                    "queries_requested": 1,
                    "queries_executed": 1,
                    "businesses_found": 1,
                    "businesses_returned": 1,
                    "duplicates_removed": 0,
                    "duration_ms": 1234,
                },
            },
        )

        first_sync = synchronize_search_run(search_run=run)
        second_sync = synchronize_search_run(search_run=run)

        self.assertEqual(first_sync.status, SearchRun.Status.COMPLETED)
        self.assertEqual(second_sync.status, SearchRun.Status.COMPLETED)
        self.assertEqual(SearchResult.objects.filter(search_run=run).count(), 1)

    def test_sync_failed_does_not_mark_run_completed(self):
        run = create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Encontrar hospitais para robótica",
            target_region="Barueri",
            selected_queries=["hospitais Barueri"],
        )
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="Executor Browser",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="browser-b",
        )
        ToolExecutorCapability.objects.create(executor=executor, tool_definition=self.maps_tool)
        claim_tool_execution(executor=executor, execution=execution)
        fail_tool_execution(executor=executor, execution=execution, error_code="executor_failed", error_message="sem navegador")

        synced = synchronize_search_run(search_run=run)

        self.assertEqual(synced.status, SearchRun.Status.FAILED)
        self.assertNotEqual(synced.status, SearchRun.Status.COMPLETED)


class ProspectingExecutionRecoveryTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant")
        self.project = Project.objects.create(tenant=self.tenant, name="Projeto A", slug="project-a")
        self.other_project = Project.objects.create(tenant=self.other_tenant, name="Projeto B", slug="project-b")
        self.definition = AgentDefinition.objects.get(slug="prospecting")
        self.version = AgentVersion.objects.get(agent_definition=self.definition, version="1.0.0")
        self.installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Prospecting A",
            configuration={"target_market": "hospitais", "target_profile": "privados", "max_results": 5},
        )
        self.other_installation = AgentInstallation.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Prospecting B",
        )
        self.plan_tool = ToolDefinition.objects.get(slug="prospecting.build_search_plan")
        self.maps_tool = ToolDefinition.objects.get(slug="prospecting.search_google_maps")
        AgentToolBinding.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            tool_definition=self.plan_tool,
            configuration={"max_queries": 4},
        )
        AgentToolBinding.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            tool_definition=self.maps_tool,
            configuration={},
        )
        AgentToolBinding.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_installation=self.other_installation,
            tool_definition=self.maps_tool,
            configuration={},
        )

    def _dispatch_run(self):
        return create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Encontrar hospitais",
            target_region="Barueri",
            selected_queries=["hospitais Barueri"],
        )

    def _executor(self, public_id="browser-recovery"):
        executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="Executor Browser",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id=public_id,
        )
        ToolExecutorCapability.objects.create(executor=executor, tool_definition=self.maps_tool)
        return executor

    def _fail_run(self, run, error_code="results_not_loaded"):
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        executor = self._executor(public_id=f"fail-{execution.id}")
        claim_tool_execution(executor=executor, execution=execution)
        fail_tool_execution(executor=executor, execution=execution, error_code=error_code, error_message="feed vazio")
        return synchronize_search_run(search_run=run)

    def _complete_execution(self, execution, name="Hospital A", external_id="cid:1"):
        executor = execution.executor or self._executor(public_id=f"ok-{execution.id}")
        if execution.status == ToolExecution.Status.DISPATCHED:
            if execution.executor_id is None:
                ToolExecutorCapability.objects.get_or_create(executor=executor, tool_definition=self.maps_tool)
                claim_tool_execution(executor=executor, execution=execution)
                execution.refresh_from_db()
        complete_tool_execution(
            executor=execution.executor,
            execution=execution,
            result={
                "schema_version": 1,
                "status": "completed",
                "businesses": [
                    {
                        "name": name,
                        "category": None,
                        "address": "Rua A, 1",
                        "phone": None,
                        "website": None,
                        "maps_url": f"https://maps.google.com/?cid={external_id}",
                        "external_id": external_id,
                        "source_query": "hospitais Barueri",
                    }
                ],
                "stats": {
                    "queries_requested": 1,
                    "queries_executed": 1,
                    "businesses_found": 1,
                    "businesses_returned": 1,
                    "duplicates_removed": 0,
                    "duration_ms": 10,
                },
            },
        )
        return execution

    def test_failed_run_allows_retry_and_preserves_previous_attempt(self):
        run = self._fail_run(self._dispatch_run())
        first_execution_id = run.agent_platform_execution_id
        self.assertEqual(run.status, SearchRun.Status.FAILED)

        first = retry_search_run(search_run=run)
        second = retry_search_run(search_run=first.search_run)

        self.assertTrue(first.created_new_attempt)
        self.assertFalse(second.created_new_attempt)
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=run).count(), 2)
        self.assertEqual(first.attempt.attempt_number, 2)
        self.assertEqual(first.attempt.tool_execution_id, second.attempt.tool_execution_id)
        self.assertNotEqual(first.attempt.tool_execution_id, first_execution_id)
        self.assertTrue(
            SearchRunExecutionAttempt.objects.filter(search_run=run, tool_execution_id=first_execution_id, attempt_number=1).exists()
        )
        first.search_run.refresh_from_db()
        self.assertEqual(first.search_run.status, SearchRun.Status.DISPATCHED)
        self.assertEqual(first.search_run.project_id, self.project.id)
        self.assertEqual(first.search_run.agent_installation_id, self.installation.id)
        self.assertEqual(
            first.attempt.tool_execution.idempotency_key,
            build_search_run_attempt_idempotency_key(run=run, attempt_number=2),
        )

    def test_succeeded_and_running_do_not_allow_retry(self):
        run = self._dispatch_run()
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        self._complete_execution(execution)
        completed = synchronize_search_run(search_run=run)
        with self.assertRaises(ProspectingExecutionRecoveryError):
            retry_search_run(search_run=completed)

        running = self._dispatch_run()
        running_execution = ToolExecution.objects.get(pk=running.agent_platform_execution_id)
        executor = self._executor(public_id=f"run-{running_execution.id}")
        claim_tool_execution(executor=executor, execution=running_execution)
        running = synchronize_search_run(search_run=running)
        self.assertEqual(running.status, SearchRun.Status.RUNNING)
        outcome = retry_search_run(search_run=running)
        self.assertFalse(outcome.created_new_attempt)
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=running).count(), 1)

    def test_dispatched_does_not_create_new_attempt(self):
        run = self._dispatch_run()
        outcome = retry_search_run(search_run=run)
        self.assertFalse(outcome.created_new_attempt)
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=run).count(), 1)

    def test_retry_then_success_completes_run_and_sync_uses_current_attempt(self):
        run = self._fail_run(self._dispatch_run())
        first_execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        outcome = retry_search_run(search_run=run)
        new_execution = ToolExecution.objects.get(pk=outcome.attempt.tool_execution_id)
        self._complete_execution(new_execution, name="Hospital B", external_id="cid:2")
        first_execution.error_code = "stale_should_be_ignored"
        first_execution.save(update_fields=["error_code"])
        synced = synchronize_search_run(search_run=run)
        self.assertEqual(synced.status, SearchRun.Status.COMPLETED)
        self.assertEqual(synced.agent_platform_execution_id, new_execution.id)
        self.assertEqual(SearchResult.objects.filter(search_run=run).count(), 1)
        synchronize_search_run(search_run=run)
        self.assertEqual(SearchResult.objects.filter(search_run=run).count(), 1)

    def test_retry_keeps_previous_results_idempotent(self):
        run = self._dispatch_run()
        first_execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        self._complete_execution(first_execution)
        synchronize_search_run(search_run=run)
        first_execution.status = ToolExecution.Status.FAILED
        first_execution.error_code = "results_not_loaded"
        first_execution.save(update_fields=["status", "error_code"])
        run.status = SearchRun.Status.FAILED
        run.save(update_fields=["status"])
        outcome = retry_search_run(search_run=run)
        second = ToolExecution.objects.get(pk=outcome.attempt.tool_execution_id)
        self._complete_execution(second, name="Hospital A", external_id="cid:1")
        synchronize_search_run(search_run=run)
        self.assertEqual(SearchResult.objects.filter(search_run=run).count(), 1)

    def test_disabled_installation_blocks_retry(self):
        run = self._fail_run(self._dispatch_run())
        self.installation.is_enabled = False
        self.installation.save(update_fields=["is_enabled", "updated_at"])
        with self.assertRaises(ProspectingExecutionRecoveryError):
            retry_search_run(search_run=run)

    def test_cancel_dispatched_is_idempotent_and_preserves_results(self):
        run = self._dispatch_run()
        SearchResult.objects.create(tenant=self.tenant, search_run=run, name="Hospital Previo")
        cancelled = cancel_search_run(search_run=run)
        cancelled_again = cancel_search_run(search_run=cancelled)
        self.assertEqual(cancelled.status, SearchRun.Status.CANCELLED)
        self.assertEqual(cancelled_again.status, SearchRun.Status.CANCELLED)
        self.assertEqual(SearchResult.objects.filter(search_run=run).count(), 1)
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=run).count(), 1)
        ToolExecution.objects.get(pk=run.agent_platform_execution_id).refresh_from_db()
        self.assertEqual(ToolExecution.objects.get(pk=run.agent_platform_execution_id).status, ToolExecution.Status.CANCELLED)

    def test_cancel_running_is_rejected(self):
        run = self._dispatch_run()
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        claim_tool_execution(executor=self._executor(public_id=f"cancel-{execution.id}"), execution=execution)
        with self.assertRaises(ProspectingExecutionRecoveryError):
            cancel_search_run(search_run=run)
        execution.refresh_from_db()
        self.assertEqual(execution.status, ToolExecution.Status.RUNNING)

    def test_cross_tenant_recovery_does_not_touch_foreign_run(self):
        run = self._fail_run(self._dispatch_run())
        foreign = SearchRun.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_installation=self.other_installation,
            target_region="Curitiba",
            queries=["escola curitiba"],
            max_results=5,
        )
        with self.assertRaises(Exception):
            retry_search_run(search_run=foreign)
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=run).count(), 1)


class ProspectingQualificationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant")
        self.project = Project.objects.create(tenant=self.tenant, name="Projeto A", slug="project-a")
        self.definition = AgentDefinition.objects.get(slug="prospecting")
        self.version = AgentVersion.objects.get(agent_definition=self.definition, version="1.0.0")
        self.installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Prospecting A",
        )
        self.search_run = SearchRun.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            target_region="São Paulo",
            queries=["hospital privado são paulo"],
            max_results=10,
        )
        self.search_result = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=self.search_run,
            name="Hospital Exemplo",
            website="https://hospital.example.com",
            external_id="cid:1",
            source_query="hospital privado são paulo",
        )
        self.prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=self.search_result)

    def test_qualify_prospect_updates_fields_and_audit(self):
        updated, changed = qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.HIGH,
            qualification_note="Hospital privado com site ativo.",
            actor=self.user,
        )
        self.assertTrue(changed)
        self.assertEqual(updated.qualification_status, Prospect.QualificationStatus.QUALIFIED)
        self.assertEqual(updated.priority, Prospect.Priority.HIGH)
        self.assertEqual(updated.qualification_note, "Hospital privado com site ativo.")
        self.assertIsNotNone(updated.qualified_at)
        self.assertEqual(updated.qualified_by, self.user)
        event = AuditEvent.objects.get(action=ACTION_PROSPECT_QUALIFICATION_UPDATED)
        self.assertEqual(event.before_data["qualification_status"], Prospect.QualificationStatus.UNQUALIFIED)
        self.assertEqual(event.after_data["qualification_status"], Prospect.QualificationStatus.QUALIFIED)

    def test_not_a_fit_and_on_hold_and_reversal(self):
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.NOT_A_FIT,
            priority=Prospect.Priority.LOW,
            qualification_note="Fora do perfil atual.",
            actor=self.user,
        )
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.qualification_status, Prospect.QualificationStatus.NOT_A_FIT)
        self.assertEqual(ProspectSource.objects.filter(prospect=self.prospect).count(), 1)

        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="Reavaliado.",
            actor=self.user,
        )
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.qualification_status, Prospect.QualificationStatus.QUALIFIED)

    def test_qualification_does_not_change_core_prospect_fields(self):
        before = (
            self.prospect.display_name,
            self.prospect.phone,
            self.prospect.website,
            self.prospect.maps_url,
            self.prospect.external_id,
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.ON_HOLD,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="Aguardar momento.",
            actor=self.user,
        )
        self.prospect.refresh_from_db()
        after = (
            self.prospect.display_name,
            self.prospect.phone,
            self.prospect.website,
            self.prospect.maps_url,
            self.prospect.external_id,
        )
        self.assertEqual(before, after)

    def test_cross_tenant_qualification_rejected(self):
        with self.assertRaises(ValidationError):
            qualify_prospect(
                tenant=self.other_tenant,
                prospect=self.prospect,
                qualification_status=Prospect.QualificationStatus.QUALIFIED,
                priority=Prospect.Priority.HIGH,
                qualification_note="",
                actor=self.user,
            )

    def test_idempotent_qualification_skips_audit(self):
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.HIGH,
            qualification_note="Nota.",
            actor=self.user,
        )
        self.assertEqual(AuditEvent.objects.filter(action=ACTION_PROSPECT_QUALIFICATION_UPDATED).count(), 1)
        _, changed = qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.HIGH,
            qualification_note="Nota.",
            actor=self.user,
        )
        self.assertFalse(changed)
        self.assertEqual(AuditEvent.objects.filter(action=ACTION_PROSPECT_QUALIFICATION_UPDATED).count(), 1)


class ProspectingContactTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator-contact", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant-contact")
        self.project = Project.objects.create(tenant=self.tenant, name="Projeto A", slug="project-a-contact")
        self.other_project = Project.objects.create(tenant=self.other_tenant, name="Projeto B", slug="project-b-contact")
        self.definition = AgentDefinition.objects.get(slug="prospecting")
        self.version = AgentVersion.objects.get(agent_definition=self.definition, version="1.0.0")
        self.installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Prospecting Contact",
        )
        self.other_installation = AgentInstallation.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_definition=self.definition,
            agent_version=self.version,
            name="Prospecting Other",
        )
        self.prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Hospital ABC",
            identity_key="hospital-abc",
        )
        self.other_prospect = Prospect.objects.create(
            tenant=self.other_tenant,
            display_name="Hospital XYZ",
            identity_key="hospital-xyz",
        )

    def test_create_contact_and_normalize_email_phone(self):
        contact = create_prospect_contact(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
            role_title="Compras",
            email=" Maria@HospitalABC.com ",
            phone="(11) 99999-0000",
            note="Canal principal.",
            actor=self.user,
        )
        self.assertEqual(contact.normalized_email, "maria@hospitalabc.com")
        self.assertEqual(contact.normalized_phone, "11999990000")
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_CONTACT_CREATED).exists())

    def test_contact_requires_identifier(self):
        with self.assertRaises(ValidationError):
            create_prospect_contact(
                tenant=self.tenant,
                prospect=self.prospect,
                name="",
                email="",
                phone="",
                actor=self.user,
            )

    def test_email_only_and_name_only_contacts(self):
        email_only = create_prospect_contact(
            tenant=self.tenant,
            prospect=self.prospect,
            email="compras@empresa.com.br",
            actor=self.user,
        )
        name_only = create_prospect_contact(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Recepção",
            actor=self.user,
        )
        self.assertEqual(email_only.email, "compras@empresa.com.br")
        self.assertEqual(name_only.name, "Recepção")

    def test_duplicate_email_or_phone_on_same_prospect_rejected(self):
        create_prospect_contact(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria",
            email="maria@example.com",
            actor=self.user,
        )
        with self.assertRaises(ValidationError):
            create_prospect_contact(
                tenant=self.tenant,
                prospect=self.prospect,
                name="Maria Silva",
                email="MARIA@example.com",
                actor=self.user,
            )
        create_prospect_contact(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Comercial",
            phone="11988887777",
            actor=self.user,
        )
        with self.assertRaises(ValidationError):
            create_prospect_contact(
                tenant=self.tenant,
                prospect=self.prospect,
                name="Outro",
                phone="(11) 98888-7777",
                actor=self.user,
            )

    def test_same_email_allowed_on_different_prospects_and_tenants(self):
        create_prospect_contact(
            tenant=self.tenant,
            prospect=self.prospect,
            email="shared@example.com",
            actor=self.user,
        )
        create_prospect_contact(
            tenant=self.other_tenant,
            prospect=self.other_prospect,
            email="shared@example.com",
            actor=self.user,
        )
        other = Prospect.objects.create(tenant=self.tenant, display_name="Clinica B", identity_key="clinica-b")
        create_prospect_contact(
            tenant=self.tenant,
            prospect=other,
            email="shared@example.com",
            actor=self.user,
        )
        self.assertEqual(ProspectContact.objects.filter(normalized_email="shared@example.com").count(), 3)

    def test_same_name_without_strong_identifier_does_not_dedupe(self):
        create_prospect_contact(tenant=self.tenant, prospect=self.prospect, name="Maria Silva", actor=self.user)
        create_prospect_contact(tenant=self.tenant, prospect=self.prospect, name="Maria Silva", actor=self.user)
        self.assertEqual(ProspectContact.objects.filter(prospect=self.prospect, name="Maria Silva").count(), 2)

    def test_update_edit_and_delete_with_audit(self):
        contact = create_prospect_contact(
            tenant=self.tenant,
            prospect=self.prospect,
            name="João Souza",
            role_title="TI",
            email="joao@example.com",
            actor=self.user,
        )
        before_name = self.prospect.display_name
        updated, changed = update_prospect_contact(
            tenant=self.tenant,
            contact=contact,
            name="João Souza",
            role_title="Infraestrutura",
            email="joao@example.com",
            phone="11999991111",
            note="",
            actor=self.user,
        )
        self.assertTrue(changed)
        self.assertEqual(updated.role_title, "Infraestrutura")
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_CONTACT_UPDATED).exists())
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.display_name, before_name)

        delete_prospect_contact(tenant=self.tenant, contact=updated, actor=self.user)
        self.assertFalse(ProspectContact.objects.filter(pk=contact.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_CONTACT_DELETED).exists())
        self.assertEqual(ProspectEnrichment.objects.filter(prospect=self.prospect).count(), 0)

    def test_cross_tenant_contact_mutation_rejected(self):
        contact = create_prospect_contact(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Contato",
            email="contato@example.com",
            actor=self.user,
        )
        with self.assertRaises(ValidationError):
            update_prospect_contact(
                tenant=self.other_tenant,
                contact=contact,
                name="Hack",
                role_title="",
                email="contato@example.com",
                phone="",
                note="",
                actor=self.user,
            )


class ProspectingBootstrapCommandTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        self.definition = AgentDefinition.objects.get(slug="prospecting")
        self.version = AgentVersion.objects.get(agent_definition=self.definition, status=AgentVersion.Status.ACTIVE)
        self.plan_tool = ToolDefinition.objects.get(slug="prospecting.build_search_plan")
        self.maps_tool = ToolDefinition.objects.get(slug="prospecting.search_google_maps")

    def test_bootstrap_is_idempotent_and_tenant_scoped(self):
        call_command(
            "bootstrap_prospecting_operations",
            tenant_slug=self.tenant.slug,
            project_slug="prospeccao-comercial",
            project_name="Prospecção Comercial",
            ensure_executor=True,
            apply=True,
        )
        call_command(
            "bootstrap_prospecting_operations",
            tenant_slug=self.tenant.slug,
            project_slug="prospeccao-comercial",
            project_name="Prospecção Comercial",
            ensure_executor=True,
            apply=True,
        )

        project = Project.objects.get(tenant=self.tenant, slug="prospeccao-comercial")
        installation = AgentInstallation.objects.get(tenant=self.tenant, project=project, agent_definition=self.definition)
        self.assertTrue(installation.is_enabled)
        self.assertEqual(AgentInstallation.objects.filter(tenant=self.tenant, project=project, agent_definition=self.definition).count(), 1)

        plan_bindings = AgentToolBinding.objects.filter(
            tenant=self.tenant,
            project=project,
            agent_installation=installation,
            tool_definition=self.plan_tool,
            is_enabled=True,
        )
        maps_bindings = AgentToolBinding.objects.filter(
            tenant=self.tenant,
            project=project,
            agent_installation=installation,
            tool_definition=self.maps_tool,
            is_enabled=True,
        )
        self.assertEqual(plan_bindings.count(), 1)
        self.assertEqual(maps_bindings.count(), 1)

        executor = ToolExecutor.objects.get(tenant=self.tenant, name="SCB Chrome Executor Operacional")
        self.assertTrue(executor.is_active)
        self.assertTrue(
            ToolExecutorCapability.objects.filter(
                executor=executor,
                tool_definition=self.maps_tool,
                is_enabled=True,
            ).exists()
        )
        self.assertEqual(ToolExecutorCredential.objects.filter(executor=executor).count(), 0)

    def test_bootstrap_dry_run_does_not_persist(self):
        output = StringIO()
        call_command(
            "bootstrap_prospecting_operations",
            tenant_slug=self.tenant.slug,
            project_slug="prospeccao-comercial",
            project_name="Prospecção Comercial",
            ensure_executor=True,
            dry_run=True,
            stdout=output,
        )
        self.assertIn("DRY RUN - nenhuma alteração gravada", output.getvalue())
        self.assertFalse(Project.objects.filter(tenant=self.tenant, slug="prospeccao-comercial").exists())
