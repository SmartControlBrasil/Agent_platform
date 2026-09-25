from __future__ import annotations

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from prospecting.application.enrichments import add_prospect_enrichment
from prospecting.application.prospects import promote_search_result_to_prospect
from prospecting.application.review import (
    ProspectingReviewError,
    bulk_ignore_search_results,
    bulk_promote_search_results,
    mark_search_result_ignored,
    restore_search_result_to_unreviewed,
)
from prospecting.application.search_runs import (
    build_search_plan_for_manual_run,
    create_and_dispatch_search_run,
    synchronize_search_run,
)
from prospecting.application.website_enrichment import enrich_prospect_from_website
from prospecting.infrastructure.website_fetcher import validate_public_http_url
from prospecting.interfaces.website import WebsiteFetchResult
from prospecting.models import Prospect, ProspectEnrichment, ProspectSource, SearchResult, SearchRun
from tenants.models import Tenant
from tools.application.lifecycle import claim_tool_execution, complete_tool_execution, fail_tool_execution
from tools.models import AgentToolBinding, ToolDefinition, ToolExecution, ToolExecutor, ToolExecutorCapability


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
