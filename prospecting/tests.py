from __future__ import annotations

import uuid
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from io import StringIO

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from prospecting.application.enrichments import add_prospect_enrichment
from prospecting.application.prospects import promote_search_result_to_prospect
from prospecting.application.activities import (
    ACTION_PROSPECT_ACTIVITY_CREATED,
    ACTION_PROSPECT_ACTIVITY_DELETED,
    ACTION_PROSPECT_ACTIVITY_UPDATED,
    create_prospect_activity,
    delete_prospect_activity,
    update_prospect_activity,
)
from prospecting.application.contacts import (
    ACTION_PROSPECT_CONTACT_CREATED,
    ACTION_PROSPECT_CONTACT_DELETED,
    ACTION_PROSPECT_CONTACT_UPDATED,
    create_prospect_contact,
    delete_prospect_contact,
    update_prospect_contact,
)
from prospecting.application.ports.email_sender import EmailSendResult
from prospecting.application.outreach_drafts import (
    ACTION_OUTREACH_DRAFT_ARCHIVED,
    ACTION_OUTREACH_DRAFT_CREATED,
    ACTION_OUTREACH_DRAFT_READY,
    ACTION_OUTREACH_DRAFT_UPDATED,
    archive_outreach_draft,
    create_outreach_draft,
    mark_outreach_draft_ready,
    revert_outreach_draft_to_draft,
    update_outreach_draft,
)
from prospecting.application.contact_outcomes import (
    ACTION_CONTACT_OUTCOME_CREATED,
    record_prospect_contact_outcome,
)
from prospecting.application.follow_ups import (
    ACTION_FOLLOW_UP_CANCELLED,
    ACTION_FOLLOW_UP_COMPLETED,
    ACTION_FOLLOW_UP_CREATED,
    cancel_prospect_follow_up,
    complete_prospect_follow_up,
    create_prospect_follow_up,
)
from prospecting.application.outreach_sends import (
    ACTION_OUTREACH_SEND_FAILED,
    ACTION_OUTREACH_SEND_REQUESTED,
    ACTION_OUTREACH_SEND_RETRY_REQUESTED,
    ACTION_OUTREACH_SEND_SENT,
    retry_outreach_send,
    send_outreach_draft_email,
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
    redispatch_search_run,
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
from prospecting.models import (
    Prospect,
    ProspectActivity,
    ProspectContact,
    ProspectEnrichment,
    ProspectContactOutcome,
    ProspectFollowUp,
    ProspectOutreachDraft,
    ProspectOutreachSend,
    ProspectSource,
    SearchResult,
    SearchRun,
    SearchRunExecutionAttempt,
)
from django.utils import timezone
from datetime import timedelta
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
        self.assertEqual(prospect.phone, self.search_result.phone)
        self.assertEqual(prospect.address, self.search_result.address)
        self.assertTrue(
            ProspectEnrichment.objects.filter(
                prospect=prospect,
                field=ProspectEnrichment.Field.PHONE,
                source_type=ProspectEnrichment.SourceType.SEARCH_RESULT,
            ).exists()
        )
        self.assertTrue(
            ProspectEnrichment.objects.filter(
                prospect=prospect,
                field=ProspectEnrichment.Field.ADDRESS,
                source_type=ProspectEnrichment.SourceType.SEARCH_RESULT,
            ).exists()
        )

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
        self.assertEqual(
            ProspectEnrichment.objects.filter(prospect=prospect, field=ProspectEnrichment.Field.EMAIL).count(),
            1,
        )

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
        self.assertEqual(
            ProspectEnrichment.objects.filter(
                prospect=prospect,
                field=ProspectEnrichment.Field.PHONE,
                source_type=ProspectEnrichment.SourceType.WEBSITE,
            ).count(),
            2,
        )
        self.assertEqual(
            ProspectEnrichment.objects.filter(prospect=prospect, field=ProspectEnrichment.Field.PHONE).count(),
            3,
        )

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
        email_count = ProspectEnrichment.objects.filter(prospect=prospect, field=ProspectEnrichment.Field.EMAIL).count()
        enrich_prospect_from_website(tenant=self.tenant, prospect=prospect, fetcher=FakeFetcher())
        self.assertEqual(
            ProspectEnrichment.objects.filter(prospect=prospect, field=ProspectEnrichment.Field.EMAIL).count(),
            email_count,
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

    @override_settings(
        WEBSITE_ENRICHMENT_MAX_PAGES=5,
        WEBSITE_ENRICHMENT_CONNECT_TIMEOUT=1,
        WEBSITE_ENRICHMENT_READ_TIMEOUT=1,
        WEBSITE_ENRICHMENT_MAX_REDIRECTS=2,
        WEBSITE_ENRICHMENT_MAX_BODY_BYTES=200000,
        WEBSITE_ENRICHMENT_USER_AGENT="AgentPlatformWebsiteEnrichment/1.0",
    )
    def test_website_enrichment_seed_404_does_not_abort_when_unit_page_works(self):
        prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Hospital Municipal",
            identity_key="portal.example.gov.br:hospital-municipal",
            website="https://portal.example.gov.br/cidadao/saude/hospital-municipal",
        )

        class Seed404Fetcher:
            def fetch(self, url):
                if url.endswith("/cidadao/saude/hospital-municipal"):
                    raise ValidationError("Website fetch failed with HTTP 404.")
                if url.endswith("/contato"):
                    return WebsiteFetchResult(
                        url=url,
                        status_code=200,
                        content_type="text/html",
                        body="Telefone (11) 2575-3200 contato@hospital.example.gov.br",
                    )
                if url == "https://portal.example.gov.br":
                    return WebsiteFetchResult(
                        url=url,
                        status_code=200,
                        content_type="text/html",
                        body='<a href="/contato">Contato</a>',
                    )
                raise ValidationError("Website fetch failed with HTTP 404.")

        result = enrich_prospect_from_website(tenant=self.tenant, prospect=prospect, fetcher=Seed404Fetcher())
        self.assertGreaterEqual(result.pages_succeeded, 1)
        self.assertTrue(result.warnings)
        self.assertTrue(
            ProspectEnrichment.objects.filter(
                prospect=prospect,
                field=ProspectEnrichment.Field.EMAIL,
                normalized_value="contato@hospital.example.gov.br",
            ).exists()
        )


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
        self.assertIsNotNone(execution.expires_at)
        self.assertTrue(timezone.is_aware(execution.expires_at))
        self.assertGreater(execution.expires_at, timezone.now())

    @override_settings(PROSPECTING_SEARCH_EXECUTION_TIMEOUT_MINUTES=7)
    def test_search_dispatch_uses_configured_timeout(self):
        before = timezone.now()
        run = create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Encontrar hospitais para robótica",
            target_region="Barueri",
            selected_queries=["hospitais Barueri"],
        )
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        self.assertGreaterEqual(execution.expires_at, before + timedelta(minutes=6, seconds=55))
        self.assertLessEqual(execution.expires_at, timezone.now() + timedelta(minutes=7, seconds=5))

    def test_build_plan_tool_does_not_receive_search_timeout(self):
        build_search_plan_for_manual_run(
            tenant=self.tenant,
            project=self.project,
            objective="Encontrar hospitais para robótica",
            target_region="Barueri",
        )
        execution = ToolExecution.objects.get(tool_definition=self.plan_tool)
        self.assertIsNone(execution.expires_at)

    def test_sync_expired_execution_marks_run_failed(self):
        run = create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Encontrar hospitais para robótica",
            target_region="Barueri",
            selected_queries=["hospitais Barueri"],
        )
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        execution.status = ToolExecution.Status.EXPIRED
        execution.error_code = "tool_execution_expired"
        execution.error_message = "A execução expirou antes de claim."
        execution.completed_at = timezone.now()
        execution.save(update_fields=["status", "error_code", "error_message", "completed_at", "updated_at"])

        synced = synchronize_search_run(search_run=run)

        self.assertEqual(synced.status, SearchRun.Status.FAILED)
        self.assertEqual(synced.error_code, "tool_execution_expired")

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

    def test_redispatch_dispatched_is_idempotent_and_does_not_duplicate_attempts(self):
        run = self._dispatch_run()
        first_execution_id = run.agent_platform_execution_id
        first = redispatch_search_run(search_run=run)
        second = redispatch_search_run(search_run=first.search_run)

        self.assertFalse(first.transitioned_to_dispatched)
        self.assertFalse(second.transitioned_to_dispatched)
        self.assertEqual(first.attempt.tool_execution_id, first_execution_id)
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=run).count(), 1)

    def test_redispatch_pending_reuses_attempt_and_sets_timeout(self):
        run = self._dispatch_run()
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        execution.status = ToolExecution.Status.PENDING
        execution.expires_at = None
        execution.save(update_fields=["status", "expires_at", "updated_at"])
        run.status = SearchRun.Status.PENDING
        run.save(update_fields=["status", "updated_at"])

        outcome = redispatch_search_run(search_run=run)

        execution.refresh_from_db()
        self.assertTrue(outcome.transitioned_to_dispatched)
        self.assertEqual(execution.status, ToolExecution.Status.DISPATCHED)
        self.assertIsNotNone(execution.expires_at)
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=run).count(), 1)

    def test_expired_run_retry_creates_unique_attempt(self):
        run = self._dispatch_run()
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        execution.status = ToolExecution.Status.EXPIRED
        execution.error_code = "tool_execution_expired"
        execution.save(update_fields=["status", "error_code", "updated_at"])
        run = synchronize_search_run(search_run=run)

        outcome = retry_search_run(search_run=run)

        self.assertTrue(outcome.created_new_attempt)
        self.assertEqual(outcome.attempt.attempt_number, 2)
        self.assertNotEqual(outcome.attempt.tool_execution_id, execution.id)
        self.assertEqual(
            outcome.attempt.tool_execution.idempotency_key,
            build_search_run_attempt_idempotency_key(run=run, attempt_number=2),
        )
        self.assertIsNotNone(outcome.attempt.tool_execution.expires_at)

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

    def test_expire_tool_executions_command_expires_due_and_syncs_runs_idempotently(self):
        run = self._dispatch_run()
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        execution.expires_at = timezone.now() - timedelta(minutes=1)
        execution.save(update_fields=["expires_at", "updated_at"])
        valid = self._dispatch_run()
        valid_execution = ToolExecution.objects.get(pk=valid.agent_platform_execution_id)
        valid_execution.expires_at = timezone.now() + timedelta(minutes=5)
        valid_execution.save(update_fields=["expires_at", "updated_at"])
        terminal = self._fail_run(self._dispatch_run())
        terminal_execution = ToolExecution.objects.get(pk=terminal.agent_platform_execution_id)
        terminal_execution.expires_at = timezone.now() - timedelta(minutes=1)
        terminal_execution.save(update_fields=["expires_at", "updated_at"])

        out = StringIO()
        call_command("expire_tool_executions", stdout=out)
        call_command("expire_tool_executions", stdout=StringIO())

        execution.refresh_from_db()
        valid_execution.refresh_from_db()
        terminal_execution.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(execution.status, ToolExecution.Status.EXPIRED)
        self.assertEqual(run.status, SearchRun.Status.FAILED)
        self.assertEqual(valid_execution.status, ToolExecution.Status.DISPATCHED)
        self.assertEqual(terminal_execution.status, ToolExecution.Status.FAILED)
        self.assertIn("expired=1", out.getvalue())

    def test_expire_tool_executions_command_respects_tenant_scope_and_dry_run(self):
        run = self._dispatch_run()
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        execution.expires_at = timezone.now() - timedelta(minutes=1)
        execution.save(update_fields=["expires_at", "updated_at"])
        foreign = SearchRun.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_installation=self.other_installation,
            target_region="Curitiba",
            queries=["escola curitiba"],
            max_results=5,
        )
        foreign_execution = ToolExecution.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_installation=self.other_installation,
            tool_binding=AgentToolBinding.objects.get(tenant=self.other_tenant, tool_definition=self.maps_tool),
            tool_definition=self.maps_tool,
            execution_mode=ToolDefinition.ExecutionMode.DELEGATED,
            status=ToolExecution.Status.DISPATCHED,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        SearchRunExecutionAttempt.objects.create(
            tenant=self.other_tenant, search_run=foreign, tool_execution=foreign_execution, attempt_number=1
        )

        call_command("expire_tool_executions", tenant_slug=self.tenant.slug, dry_run=True, stdout=StringIO())
        execution.refresh_from_db()
        self.assertEqual(execution.status, ToolExecution.Status.DISPATCHED)

        call_command("expire_tool_executions", tenant_slug=self.tenant.slug, stdout=StringIO())

        execution.refresh_from_db()
        foreign_execution.refresh_from_db()
        self.assertEqual(execution.status, ToolExecution.Status.EXPIRED)
        self.assertEqual(foreign_execution.status, ToolExecution.Status.DISPATCHED)


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


class ProspectingActivityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator-activity", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-act")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant-act")
        self.prospect = Prospect.objects.create(tenant=self.tenant, display_name="Hospital ABC", identity_key="hospital-abc-act")
        self.other_prospect = Prospect.objects.create(tenant=self.other_tenant, display_name="Outro", identity_key="outro-act")
        self.contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
            email="maria@hospital.example.com",
        )
        self.other_contact = ProspectContact.objects.create(
            tenant=self.other_tenant,
            prospect=self.other_prospect,
            name="Outro Contato",
            email="outro@example.com",
        )

    def test_create_note_and_call_with_contact(self):
        note = create_prospect_activity(
            tenant=self.tenant,
            prospect=self.prospect,
            activity_type=ProspectActivity.ActivityType.NOTE,
            note="Hospital com forte potencial.",
            actor=self.user,
        )
        self.assertEqual(note.created_by, self.user)
        self.assertIsNone(note.contact)

        call = create_prospect_activity(
            tenant=self.tenant,
            prospect=self.prospect,
            activity_type=ProspectActivity.ActivityType.CALL,
            note="Apresentação inicial realizada.",
            contact=self.contact,
            occurred_at=timezone.now() - timedelta(hours=2),
            actor=self.user,
        )
        self.assertEqual(call.contact_id, self.contact.id)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_ACTIVITY_CREATED).exists())

    def test_note_required_and_wrong_contact_rejected(self):
        with self.assertRaises(ValidationError):
            create_prospect_activity(
                tenant=self.tenant,
                prospect=self.prospect,
                activity_type=ProspectActivity.ActivityType.NOTE,
                note="   ",
                actor=self.user,
            )
        with self.assertRaises(ValidationError):
            create_prospect_activity(
                tenant=self.tenant,
                prospect=self.prospect,
                activity_type=ProspectActivity.ActivityType.CALL,
                note="Ligação",
                contact=self.other_contact,
                actor=self.user,
            )

    def test_cross_tenant_rejected(self):
        activity = create_prospect_activity(
            tenant=self.tenant,
            prospect=self.prospect,
            activity_type=ProspectActivity.ActivityType.NOTE,
            note="Nota",
            actor=self.user,
        )
        with self.assertRaises(ValidationError):
            update_prospect_activity(
                tenant=self.other_tenant,
                activity=activity,
                activity_type=ProspectActivity.ActivityType.NOTE,
                note="Hack",
                occurred_at=activity.occurred_at,
                actor=self.user,
            )

    def test_update_delete_and_qualification_unchanged(self):
        status_before = self.prospect.qualification_status
        priority_before = self.prospect.priority
        activity = create_prospect_activity(
            tenant=self.tenant,
            prospect=self.prospect,
            activity_type=ProspectActivity.ActivityType.MEETING,
            note="Reunião inicial.",
            actor=self.user,
        )
        updated, changed = update_prospect_activity(
            tenant=self.tenant,
            activity=activity,
            activity_type=ProspectActivity.ActivityType.MEETING,
            note="Reunião inicial — follow-up pedido.",
            occurred_at=activity.occurred_at,
            contact=self.contact,
            actor=self.user,
        )
        self.assertTrue(changed)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_ACTIVITY_UPDATED).exists())
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.qualification_status, status_before)
        self.assertEqual(self.prospect.priority, priority_before)
        self.assertEqual(ProspectEnrichment.objects.filter(prospect=self.prospect).count(), 0)

        delete_prospect_activity(tenant=self.tenant, activity=updated, actor=self.user)
        self.assertFalse(ProspectActivity.objects.filter(pk=activity.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_ACTIVITY_DELETED).exists())


class ProspectingOutreachDraftTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator-outreach", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-outreach")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant-outreach")
        self.prospect = Prospect.objects.create(tenant=self.tenant, display_name="Hospital ABC", identity_key="hospital-abc-out")
        self.other_prospect = Prospect.objects.create(tenant=self.other_tenant, display_name="Outro", identity_key="outro-out")
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        self.email_contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
            email="maria@hospital.example.com",
        )
        self.phone_contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="João Souza",
            phone="11999990000",
        )
        self.other_contact = ProspectContact.objects.create(
            tenant=self.other_tenant,
            prospect=self.other_prospect,
            name="Outro Contato",
            email="outro@example.com",
        )

    def test_create_email_and_phone_drafts(self):
        email_draft = create_outreach_draft(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.email_contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            subject="Soluções robóticas",
            body="Prezada Maria, ...",
            actor=self.user,
        )
        self.assertEqual(email_draft.destination_email, "maria@hospital.example.com")
        self.assertEqual(email_draft.status, ProspectOutreachDraft.Status.DRAFT)

        phone_draft = create_outreach_draft(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.phone_contact,
            channel=ProspectOutreachDraft.Channel.PHONE,
            body="Roteiro: apresentar soluções.",
            actor=self.user,
        )
        self.assertEqual(phone_draft.destination_phone, "11999990000")
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_DRAFT_CREATED).exists())

    def test_email_requires_contact_email_and_subject(self):
        no_email = ProspectContact.objects.create(tenant=self.tenant, prospect=self.prospect, name="Sem Email")
        with self.assertRaises(ValidationError):
            create_outreach_draft(
                tenant=self.tenant,
                prospect=self.prospect,
                contact=no_email,
                channel=ProspectOutreachDraft.Channel.EMAIL,
                subject="Assunto",
                body="Corpo",
                actor=self.user,
            )
        with self.assertRaises(ValidationError):
            create_outreach_draft(
                tenant=self.tenant,
                prospect=self.prospect,
                contact=self.email_contact,
                channel=ProspectOutreachDraft.Channel.EMAIL,
                subject="",
                body="Corpo",
                actor=self.user,
            )

    def test_phone_requires_contact_phone(self):
        with self.assertRaises(ValidationError):
            create_outreach_draft(
                tenant=self.tenant,
                prospect=self.prospect,
                contact=self.email_contact,
                channel=ProspectOutreachDraft.Channel.PHONE,
                body="Script",
                actor=self.user,
            )

    def test_cross_prospect_and_cross_tenant_rejected(self):
        with self.assertRaises(ValidationError):
            create_outreach_draft(
                tenant=self.tenant,
                prospect=self.prospect,
                contact=self.other_contact,
                channel=ProspectOutreachDraft.Channel.EMAIL,
                subject="Hack",
                body="Corpo",
                actor=self.user,
            )
        draft = create_outreach_draft(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.email_contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            subject="Assunto",
            body="Corpo",
            actor=self.user,
        )
        with self.assertRaises(ValidationError):
            mark_outreach_draft_ready(tenant=self.other_tenant, draft=draft, actor=self.user)

    def test_body_required_and_status_transitions(self):
        with self.assertRaises(ValidationError):
            create_outreach_draft(
                tenant=self.tenant,
                prospect=self.prospect,
                contact=self.email_contact,
                channel=ProspectOutreachDraft.Channel.EMAIL,
                subject="Assunto",
                body="   ",
                actor=self.user,
            )
        draft = create_outreach_draft(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.email_contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            subject="Assunto",
            body="Corpo",
            actor=self.user,
        )
        mark_outreach_draft_ready(tenant=self.tenant, draft=draft, actor=self.user)
        draft.refresh_from_db()
        self.assertEqual(draft.status, ProspectOutreachDraft.Status.READY)
        self.assertIsNotNone(draft.prepared_at)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_DRAFT_READY).exists())

        revert_outreach_draft_to_draft(tenant=self.tenant, draft=draft, actor=self.user)
        draft.refresh_from_db()
        self.assertEqual(draft.status, ProspectOutreachDraft.Status.DRAFT)

        archive_outreach_draft(tenant=self.tenant, draft=draft, actor=self.user)
        draft.refresh_from_db()
        self.assertEqual(draft.status, ProspectOutreachDraft.Status.ARCHIVED)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_DRAFT_ARCHIVED).exists())

    def test_update_audit_and_prospect_activity_unchanged(self):
        self.prospect.refresh_from_db()
        activity_count_before = ProspectActivity.objects.filter(prospect=self.prospect).count()
        qual_before = self.prospect.qualification_status
        draft = create_outreach_draft(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.email_contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            subject="Assunto",
            body="Corpo original",
            actor=self.user,
        )
        updated, changed = update_outreach_draft(
            tenant=self.tenant,
            draft=draft,
            contact=self.email_contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            subject="Assunto novo",
            body="Corpo atualizado",
            actor=self.user,
        )
        self.assertTrue(changed)
        self.assertEqual(updated.subject, "Assunto novo")
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_DRAFT_UPDATED).exists())
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.qualification_status, qual_before)
        self.assertEqual(ProspectActivity.objects.filter(prospect=self.prospect).count(), activity_count_before)

    def test_unqualified_prospect_rejected(self):
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.ON_HOLD,
            priority=Prospect.Priority.LOW,
            qualification_note="",
            actor=self.user,
        )
        with self.assertRaises(ValidationError):
            create_outreach_draft(
                tenant=self.tenant,
                prospect=self.prospect,
                contact=self.email_contact,
                channel=ProspectOutreachDraft.Channel.EMAIL,
                subject="Assunto",
                body="Corpo",
                actor=self.user,
            )


class FakeOutreachEmailSender:
    def __init__(self, *results: EmailSendResult):
        self.results = list(results)
        self.calls: list[dict] = []

    def send_plain_email(self, *, to, subject, body, idempotency_key):
        self.calls.append(
            {"to": to, "subject": subject, "body": body, "idempotency_key": idempotency_key}
        )
        if not self.results:
            return EmailSendResult(success=True)
        return self.results.pop(0)


class ProspectingOutreachSendTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator-send", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-send")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant-send")
        self.prospect = Prospect.objects.create(tenant=self.tenant, display_name="Hospital ABC", identity_key="hospital-abc-send")
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        self.email_contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
            email="maria@hospital.example.com",
        )
        self.phone_contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="João Souza",
            phone="11999990000",
        )

    def _ready_email_draft(self, **kwargs):
        defaults = {
            "tenant": self.tenant,
            "prospect": self.prospect,
            "contact": self.email_contact,
            "channel": ProspectOutreachDraft.Channel.EMAIL,
            "subject": "Assunto comercial",
            "body": "Corpo da mensagem.",
            "status": ProspectOutreachDraft.Status.READY,
            "actor": self.user,
        }
        defaults.update(kwargs)
        return create_outreach_draft(**defaults)

    def test_ready_email_send_success_and_activity(self):
        draft = self._ready_email_draft()
        sender = FakeOutreachEmailSender(EmailSendResult(success=True, provider_message_id="msg-1"))
        send, delivered = send_outreach_draft_email(
            tenant=self.tenant,
            draft=draft,
            actor=self.user,
            email_sender=sender,
        )
        self.assertTrue(delivered)
        self.assertEqual(send.status, ProspectOutreachSend.Status.SENT)
        self.assertEqual(send.destination, "maria@hospital.example.com")
        self.assertEqual(send.subject_snapshot, "Assunto comercial")
        self.assertEqual(len(sender.calls), 1)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_SEND_REQUESTED).exists())
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_SEND_SENT).exists())
        self.assertEqual(self.prospect.activities.filter(activity_type=ProspectActivity.ActivityType.EMAIL_SENT).count(), 1)

    def test_draft_archived_and_phone_do_not_send(self):
        draft = self._ready_email_draft(status=ProspectOutreachDraft.Status.DRAFT)
        with self.assertRaises(ValidationError):
            send_outreach_draft_email(tenant=self.tenant, draft=draft, actor=self.user, email_sender=FakeOutreachEmailSender())
        phone = create_outreach_draft(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.phone_contact,
            channel=ProspectOutreachDraft.Channel.PHONE,
            body="Roteiro",
            status=ProspectOutreachDraft.Status.READY,
            actor=self.user,
        )
        with self.assertRaises(ValidationError):
            send_outreach_draft_email(tenant=self.tenant, draft=phone, actor=self.user, email_sender=FakeOutreachEmailSender())

    def test_double_send_is_idempotent(self):
        draft = self._ready_email_draft()
        sender = FakeOutreachEmailSender(EmailSendResult(success=True))
        send_outreach_draft_email(tenant=self.tenant, draft=draft, actor=self.user, email_sender=sender)
        _, delivered_again = send_outreach_draft_email(tenant=self.tenant, draft=draft, actor=self.user, email_sender=sender)
        self.assertFalse(delivered_again)
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(ProspectOutreachSend.objects.filter(draft=draft).count(), 1)
        self.assertEqual(self.prospect.activities.filter(activity_type=ProspectActivity.ActivityType.EMAIL_SENT).count(), 1)

    def test_technical_failure_and_retry_preserves_snapshot(self):
        draft = self._ready_email_draft()
        sender = FakeOutreachEmailSender(
            EmailSendResult(success=False, error_code="SMTPException", error_message="timeout"),
            EmailSendResult(success=True),
        )
        send, _ = send_outreach_draft_email(tenant=self.tenant, draft=draft, actor=self.user, email_sender=sender)
        self.assertEqual(send.status, ProspectOutreachSend.Status.FAILED)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_SEND_FAILED).exists())
        self.assertEqual(self.prospect.activities.count(), 0)

        update_outreach_draft(
            tenant=self.tenant,
            draft=draft,
            contact=self.email_contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            subject="Assunto alterado depois",
            body="Corpo alterado depois",
            actor=self.user,
        )
        retried, _ = retry_outreach_send(tenant=self.tenant, send=send, actor=self.user, email_sender=sender)
        self.assertEqual(retried.status, ProspectOutreachSend.Status.SENT)
        self.assertEqual(retried.subject_snapshot, "Assunto comercial")
        self.assertEqual(len(sender.calls), 2)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_SEND_RETRY_REQUESTED).exists())

    def test_cross_tenant_rejected(self):
        draft = self._ready_email_draft()
        sender = FakeOutreachEmailSender(EmailSendResult(success=True))
        send, _ = send_outreach_draft_email(tenant=self.tenant, draft=draft, actor=self.user, email_sender=sender)
        with self.assertRaises(ValidationError):
            retry_outreach_send(tenant=self.other_tenant, send=send, actor=self.user, email_sender=sender)

    def test_unqualified_prospect_blocks_send(self):
        draft = self._ready_email_draft()
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.ON_HOLD,
            priority=Prospect.Priority.LOW,
            qualification_note="",
            actor=self.user,
        )
        with self.assertRaises(ValidationError):
            send_outreach_draft_email(
                tenant=self.tenant,
                draft=draft,
                actor=self.user,
                email_sender=FakeOutreachEmailSender(EmailSendResult(success=True)),
            )


class ProspectingFollowUpWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator-follow", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-follow")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant-follow")
        self.prospect = Prospect.objects.create(tenant=self.tenant, display_name="Hospital ABC", identity_key="hospital-abc-follow")
        self.other_prospect = Prospect.objects.create(tenant=self.other_tenant, display_name="Outro", identity_key="outro-follow")
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        self.contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
            email="maria@hospital.example.com",
        )
        self.other_contact = ProspectContact.objects.create(
            tenant=self.other_tenant,
            prospect=self.other_prospect,
            name="Outro",
            email="outro@example.com",
        )

    def test_outcome_creates_activity_and_optional_follow_up(self):
        qual_before = Prospect.objects.get(pk=self.prospect.pk).qualification_status
        due = timezone.now() + timedelta(days=3)
        outcome, created, follow_up = record_prospect_contact_outcome(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.contact,
            outcome=ProspectContactOutcome.Outcome.CALLBACK_REQUESTED,
            note="Pediu retorno após reunião interna.",
            idempotency_key="outcome-key-1",
            actor=self.user,
            follow_up_action_type=ProspectFollowUp.ActionType.CALL,
            follow_up_due_at=due,
            follow_up_note="Ligar após reunião.",
            follow_up_idempotency_key="follow-key-1",
        )
        self.assertTrue(created)
        self.assertEqual(outcome.outcome, ProspectContactOutcome.Outcome.CALLBACK_REQUESTED)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_CONTACT_OUTCOME_CREATED).exists())
        self.assertEqual(self.prospect.activities.filter(activity_type=ProspectActivity.ActivityType.REPLY_RECEIVED).count(), 1)
        self.assertIsNotNone(follow_up)
        self.assertEqual(follow_up.status, ProspectFollowUp.Status.PENDING)
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.qualification_status, qual_before)

        _, created_again, _ = record_prospect_contact_outcome(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.contact,
            outcome=ProspectContactOutcome.Outcome.CALLBACK_REQUESTED,
            note="Pediu retorno após reunião interna.",
            idempotency_key="outcome-key-1",
            actor=self.user,
        )
        self.assertFalse(created_again)
        self.assertEqual(self.prospect.activities.filter(activity_type=ProspectActivity.ActivityType.REPLY_RECEIVED).count(), 1)

    def test_no_response_uses_note_activity(self):
        record_prospect_contact_outcome(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.contact,
            outcome=ProspectContactOutcome.Outcome.NO_RESPONSE,
            note="Sem retorno após 5 dias.",
            idempotency_key="outcome-key-2",
            actor=self.user,
        )
        activity = self.prospect.activities.get()
        self.assertEqual(activity.activity_type, ProspectActivity.ActivityType.NOTE)

    def test_cross_prospect_contact_rejected(self):
        with self.assertRaises(ValidationError):
            record_prospect_contact_outcome(
                tenant=self.tenant,
                prospect=self.prospect,
                contact=self.other_contact,
                outcome=ProspectContactOutcome.Outcome.INTERESTED,
                idempotency_key="outcome-key-3",
                actor=self.user,
            )

    def test_follow_up_complete_cancel_and_overdue(self):
        past_due = timezone.now() - timedelta(hours=2)
        follow_up, _ = create_prospect_follow_up(
            tenant=self.tenant,
            prospect=self.prospect,
            action_type=ProspectFollowUp.ActionType.REVIEW,
            note="Revisar proposta",
            due_at=past_due,
            contact=self.contact,
            idempotency_key="follow-key-2",
            actor=self.user,
        )
        self.assertTrue(follow_up.is_overdue)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_FOLLOW_UP_CREATED).exists())
        completed = complete_prospect_follow_up(tenant=self.tenant, follow_up=follow_up, actor=self.user)
        self.assertEqual(completed.status, ProspectFollowUp.Status.COMPLETED)
        self.assertIsNotNone(completed.completed_at)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_FOLLOW_UP_COMPLETED).exists())
        self.assertEqual(self.prospect.activities.count(), 0)

        follow_up2, _ = create_prospect_follow_up(
            tenant=self.tenant,
            prospect=self.prospect,
            action_type=ProspectFollowUp.ActionType.CALL,
            idempotency_key="follow-key-3",
            actor=self.user,
        )
        cancelled = cancel_prospect_follow_up(tenant=self.tenant, follow_up=follow_up2, actor=self.user)
        self.assertEqual(cancelled.status, ProspectFollowUp.Status.CANCELLED)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_FOLLOW_UP_CANCELLED).exists())


class ProspectingFollowUpQueueTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator-queue", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-queue")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant-queue")
        self.prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Hospital ABC",
            identity_key="hospital-abc-queue",
            priority=Prospect.Priority.HIGH,
        )
        self.other_prospect = Prospect.objects.create(
            tenant=self.other_tenant,
            display_name="Outro",
            identity_key="outro-queue",
        )
        self.contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
        )

    def _pending_at(self, due_at, **kwargs):
        follow_up, _ = create_prospect_follow_up(
            tenant=kwargs.pop("tenant", self.tenant),
            prospect=kwargs.pop("prospect", self.prospect),
            action_type=kwargs.pop("action_type", ProspectFollowUp.ActionType.CALL),
            due_at=due_at,
            contact=kwargs.pop("contact", None),
            note=kwargs.pop("note", ""),
            idempotency_key=kwargs.pop("idempotency_key", uuid.uuid4().hex),
            actor=self.user,
            **kwargs,
        )
        return follow_up

    def test_temporal_classification_and_timezone(self):
        from prospecting.application.follow_up_queue import (
            SITUATION_OVERDUE,
            SITUATION_TODAY,
            SITUATION_UPCOMING,
            classify_follow_up_temporal,
        )

        now = timezone.now()
        local_start = timezone.localtime(now).replace(hour=12, minute=0, second=0, microsecond=0)
        if timezone.is_naive(local_start):
            local_start = timezone.make_aware(local_start)
        overdue = self._pending_at(local_start - timedelta(days=1))
        today_item = self._pending_at(local_start + timedelta(hours=1))
        future = self._pending_at(local_start + timedelta(days=2))
        self.assertEqual(classify_follow_up_temporal(overdue, now=local_start + timedelta(hours=2)), SITUATION_OVERDUE)
        self.assertEqual(classify_follow_up_temporal(today_item, now=local_start), SITUATION_TODAY)
        self.assertEqual(classify_follow_up_temporal(future, now=local_start), SITUATION_UPCOMING)

    def test_pending_default_excludes_completed_and_cancelled(self):
        from prospecting.application.follow_up_queue import SITUATION_PENDING, apply_follow_up_queue_filters, base_follow_up_queue_queryset

        pending = self._pending_at(timezone.now() + timedelta(days=1))
        completed = self._pending_at(timezone.now() + timedelta(days=2), idempotency_key="q-complete")
        complete_prospect_follow_up(tenant=self.tenant, follow_up=completed, actor=self.user)
        cancelled = self._pending_at(timezone.now() + timedelta(days=3), idempotency_key="q-cancel")
        cancel_prospect_follow_up(tenant=self.tenant, follow_up=cancelled, actor=self.user)
        qs = apply_follow_up_queue_filters(
            base_follow_up_queue_queryset(tenant=self.tenant),
            situation=SITUATION_PENDING,
        )
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.get().pk, pending.pk)

    def test_ordering_overdue_oldest_first(self):
        from prospecting.application.follow_up_queue import (
            annotate_follow_up_sort_bucket,
            order_follow_up_queue,
            base_follow_up_queue_queryset,
        )

        now = timezone.now()
        older = self._pending_at(now - timedelta(days=3), idempotency_key="q-old")
        newer = self._pending_at(now - timedelta(hours=1), idempotency_key="q-new")
        self._pending_at(now + timedelta(days=1), idempotency_key="q-future")
        qs = order_follow_up_queue(annotate_follow_up_sort_bucket(base_follow_up_queue_queryset(tenant=self.tenant), now=now))
        ids = list(qs.values_list("pk", flat=True))
        self.assertEqual(ids.index(older.pk), 0)
        self.assertLess(ids.index(older.pk), ids.index(newer.pk))

    def test_tenant_isolation_and_counters(self):
        from prospecting.application.follow_up_queue import compute_follow_up_queue_counters

        fixed_now = timezone.localtime(timezone.now()).replace(hour=12, minute=0, second=0, microsecond=0)
        self._pending_at(fixed_now - timedelta(days=1), idempotency_key="q-overdue-counter")
        self._pending_at(fixed_now + timedelta(hours=2), idempotency_key="q-today-counter")
        self._pending_at(
            fixed_now + timedelta(days=1),
            tenant=self.other_tenant,
            prospect=self.other_prospect,
            idempotency_key="other-1",
        )
        counters = compute_follow_up_queue_counters(tenant=self.tenant, now=fixed_now)
        self.assertEqual(counters.pending_total, 2)
        self.assertEqual(counters.overdue, 1)
        self.assertEqual(counters.today, 1)
        other_counters = compute_follow_up_queue_counters(tenant=self.other_tenant, now=fixed_now)
        self.assertEqual(other_counters.pending_total, 1)

    def test_latest_outcome_annotation(self):
        from prospecting.application.follow_up_queue import base_follow_up_queue_queryset

        record_prospect_contact_outcome(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.contact,
            outcome=ProspectContactOutcome.Outcome.INTERESTED,
            idempotency_key="queue-outcome-1",
            actor=self.user,
        )
        self._pending_at(timezone.now() + timedelta(days=1))
        item = base_follow_up_queue_queryset(tenant=self.tenant).get()
        self.assertEqual(item.latest_outcome, ProspectContactOutcome.Outcome.INTERESTED)


class ProspectingCommercialQueueTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator-commercial-queue", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-commercial-queue")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant-commercial-queue")
        self.qualified = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Hospital Alpha",
            identity_key="hospital-alpha-cq",
            priority=Prospect.Priority.HIGH,
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.qualified,
            qualification_status=Prospect.QualificationStatus.ON_HOLD,
            priority=Prospect.Priority.HIGH,
            qualification_note="",
            actor=self.user,
        )
        self.other_qualified = Prospect.objects.create(
            tenant=self.other_tenant,
            display_name="Outro Hospital",
            identity_key="outro-hospital-cq",
        )
        qualify_prospect(
            tenant=self.other_tenant,
            prospect=self.other_qualified,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )

    def _queue(self, **kwargs):
        from prospecting.application.commercial_queue import build_commercial_queue

        return build_commercial_queue(tenant=kwargs.pop("tenant", self.tenant), **kwargs)

    def _counters(self, **kwargs):
        from prospecting.application.commercial_queue import compute_commercial_queue_counters

        return compute_commercial_queue_counters(tenant=kwargs.pop("tenant", self.tenant), **kwargs)

    def _categories_for_prospect(self, items, prospect_id):
        return [item.category for item in items if item.prospect_id == prospect_id]

    def _follow_up(self, prospect, due_at, **kwargs):
        follow_up, _ = create_prospect_follow_up(
            tenant=kwargs.pop("tenant", self.tenant),
            prospect=prospect,
            action_type=kwargs.pop("action_type", ProspectFollowUp.ActionType.CALL),
            due_at=due_at,
            idempotency_key=kwargs.pop("idempotency_key", uuid.uuid4().hex),
            actor=self.user,
            **kwargs,
        )
        return follow_up

    def _email_contact(self, prospect=None, **kwargs):
        prospect = prospect or self.qualified
        return ProspectContact.objects.create(
            tenant=prospect.tenant,
            prospect=prospect,
            name=kwargs.pop("name", "Maria Silva"),
            email=kwargs.pop("email", "maria@example.com"),
            **kwargs,
        )

    def _phone_contact(self, prospect=None, **kwargs):
        prospect = prospect or self.qualified
        return ProspectContact.objects.create(
            tenant=prospect.tenant,
            prospect=prospect,
            name=kwargs.pop("name", "João Souza"),
            phone=kwargs.pop("phone", "11999990000"),
            **kwargs,
        )

    def _email_draft(self, contact, *, status=ProspectOutreachDraft.Status.DRAFT, subject="Assunto", body="Corpo"):
        draft = create_outreach_draft(
            tenant=contact.tenant,
            prospect=contact.prospect,
            contact=contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            subject=subject,
            body=body,
            actor=self.user,
        )
        if status == ProspectOutreachDraft.Status.READY:
            mark_outreach_draft_ready(tenant=contact.tenant, draft=draft, actor=self.user)
        return draft

    def _email_send(self, draft, *, status=ProspectOutreachSend.Status.PENDING, sent_at=None):
        send = ProspectOutreachSend(
            tenant=draft.tenant,
            draft=draft,
            prospect=draft.prospect,
            contact=draft.contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            destination=draft.destination_email,
            subject_snapshot=draft.subject,
            body_snapshot=draft.body,
            status=status,
            idempotency_key=f"prospecting:outreach_send:draft:{draft.id}",
            requested_by=self.user,
        )
        if status == ProspectOutreachSend.Status.SENT:
            send.sent_at = sent_at or timezone.now()
        if status == ProspectOutreachSend.Status.FAILED:
            send.failed_at = timezone.now()
        send.save()
        return send

    def test_tenant_isolation(self):
        contact = self._email_contact(prospect=self.other_qualified, email="outro@example.com")
        self._email_draft(contact)
        items = self._queue()
        self.assertEqual(len(items), 0)
        other_items = self._queue(tenant=self.other_tenant)
        self.assertEqual(len(other_items), 1)
        self.assertEqual(other_items[0].category, "OUTREACH_PENDING")

    def test_follow_up_overdue_and_today_with_timezone(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        now = timezone.now()
        local_start = timezone.localtime(now).replace(hour=12, minute=0, second=0, microsecond=0)
        if timezone.is_naive(local_start):
            local_start = timezone.make_aware(local_start)
        overdue = self._follow_up(self.qualified, local_start - timedelta(days=1), idempotency_key="cq-overdue")
        today_item = self._follow_up(self.qualified, local_start + timedelta(hours=2), idempotency_key="cq-today")
        items = self._queue(now=local_start)
        categories = {item.follow_up_id: item.category for item in items}
        self.assertEqual(categories[overdue.id], CommercialQueueCategory.FOLLOW_UP_OVERDUE)
        self.assertEqual(categories[today_item.id], CommercialQueueCategory.FOLLOW_UP_TODAY)

    def test_unqualified_follow_up_still_appears(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        unqualified = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Sem Qualificação",
            identity_key="unqualified-cq",
        )
        due = timezone.now() - timedelta(hours=2)
        follow_up = self._follow_up(unqualified, due, idempotency_key="cq-unqualified-fu")
        items = self._queue(now=timezone.now())
        follow_items = [item for item in items if item.follow_up_id == follow_up.id]
        self.assertEqual(len(follow_items), 1)
        self.assertEqual(follow_items[0].category, CommercialQueueCategory.FOLLOW_UP_OVERDUE)

    def test_ready_with_email_or_phone_contact(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        email_prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Email Ready",
            identity_key="email-ready-cq",
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=email_prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        self._email_contact(prospect=email_prospect, email="ready@example.com")
        phone_prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Phone Ready",
            identity_key="phone-ready-cq",
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=phone_prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        self._phone_contact(prospect=phone_prospect, phone="11888887777")
        items = self._queue()
        self.assertIn(CommercialQueueCategory.READY_FOR_OUTREACH, self._categories_for_prospect(items, email_prospect.id))
        self.assertIn(CommercialQueueCategory.READY_FOR_OUTREACH, self._categories_for_prospect(items, phone_prospect.id))

    def test_missing_contact_enrichment_name_only_and_precedence(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        missing = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Missing Contact",
            identity_key="missing-contact-cq",
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=missing,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        add_prospect_enrichment(
            tenant=self.tenant,
            prospect=missing,
            field=ProspectEnrichment.Field.EMAIL,
            value="found@example.com",
            source_type=ProspectEnrichment.SourceType.WEBSITE,
            source_url="https://example.com/contato",
        )
        ProspectContact.objects.create(tenant=self.tenant, prospect=missing, name="Somente Nome")
        items = self._queue()
        self.assertEqual(
            self._categories_for_prospect(items, missing.id),
            [CommercialQueueCategory.MISSING_CONTACT],
        )

    def test_outreach_pending_draft_and_send_statuses(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        qualify_prospect(
            tenant=self.tenant,
            prospect=self.qualified,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.HIGH,
            qualification_note="",
            actor=self.user,
        )
        contact = self._email_contact(email="draft@example.com")
        draft_prospect = contact.prospect
        self._email_draft(contact, status=ProspectOutreachDraft.Status.DRAFT)
        items = self._queue()
        self.assertEqual(
            self._categories_for_prospect(items, draft_prospect.id),
            [CommercialQueueCategory.OUTREACH_PENDING],
        )

        ready_prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Ready Draft",
            identity_key="ready-draft-cq",
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=ready_prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        ready_contact = self._email_contact(prospect=ready_prospect, email="readydraft@example.com")
        self._email_draft(ready_contact, status=ProspectOutreachDraft.Status.READY)

        pending_prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Pending Send",
            identity_key="pending-send-cq",
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=pending_prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        pending_contact = self._email_contact(prospect=pending_prospect, email="pending@example.com")
        pending_draft = self._email_draft(pending_contact, status=ProspectOutreachDraft.Status.READY)
        self._email_send(pending_draft, status=ProspectOutreachSend.Status.PENDING)

        sending_prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Sending Send",
            identity_key="sending-send-cq",
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=sending_prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        sending_contact = self._email_contact(prospect=sending_prospect, email="sending@example.com")
        sending_draft = self._email_draft(sending_contact, status=ProspectOutreachDraft.Status.READY)
        self._email_send(sending_draft, status=ProspectOutreachSend.Status.SENDING)

        failed_prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Failed Send",
            identity_key="failed-send-cq",
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=failed_prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        failed_contact = self._email_contact(prospect=failed_prospect, email="failed@example.com")
        failed_draft = self._email_draft(failed_contact, status=ProspectOutreachDraft.Status.READY)
        self._email_send(failed_draft, status=ProspectOutreachSend.Status.FAILED)

        items = self._queue()
        for prospect_id in [ready_prospect.id, pending_prospect.id, sending_prospect.id, failed_prospect.id]:
            self.assertEqual(
                self._categories_for_prospect(items, prospect_id),
                [CommercialQueueCategory.OUTREACH_PENDING],
            )

    def test_waiting_outcome_and_outcome_resolution(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        qualify_prospect(
            tenant=self.tenant,
            prospect=self.qualified,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.HIGH,
            qualification_note="",
            actor=self.user,
        )
        contact = self._email_contact(email="waiting@example.com")
        draft = self._email_draft(contact, status=ProspectOutreachDraft.Status.READY)
        send = self._email_send(draft, status=ProspectOutreachSend.Status.SENT)
        items = self._queue()
        self.assertEqual(
            self._categories_for_prospect(items, contact.prospect_id),
            [CommercialQueueCategory.WAITING_OUTCOME],
        )

        record_prospect_contact_outcome(
            tenant=self.tenant,
            prospect=contact.prospect,
            contact=contact,
            outcome=ProspectContactOutcome.Outcome.AWAITING_RESPONSE,
            outreach_send=send,
            idempotency_key="cq-awaiting-outcome",
            actor=self.user,
        )
        items_after = self._queue()
        self.assertNotIn(
            CommercialQueueCategory.WAITING_OUTCOME,
            self._categories_for_prospect(items_after, contact.prospect_id),
        )
        self.assertIn(
            CommercialQueueCategory.READY_FOR_OUTREACH,
            self._categories_for_prospect(items_after, contact.prospect_id),
        )

    def test_phone_draft_ready_without_send_is_outreach_pending_not_waiting(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        qualify_prospect(
            tenant=self.tenant,
            prospect=self.qualified,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.HIGH,
            qualification_note="",
            actor=self.user,
        )
        contact = self._phone_contact()
        create_outreach_draft(
            tenant=self.tenant,
            prospect=self.qualified,
            contact=contact,
            channel=ProspectOutreachDraft.Channel.PHONE,
            body="Ligar para apresentar solução.",
            actor=self.user,
        )
        mark_outreach_draft_ready(tenant=self.tenant, draft=ProspectOutreachDraft.objects.get(contact=contact), actor=self.user)
        items = self._queue()
        categories = self._categories_for_prospect(items, self.qualified.id)
        self.assertEqual(categories, [CommercialQueueCategory.OUTREACH_PENDING])
        self.assertNotIn(CommercialQueueCategory.WAITING_OUTCOME, categories)

    def test_non_qualified_prospects_excluded_from_prospect_level_categories(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        for status, label in [
            (Prospect.QualificationStatus.UNQUALIFIED, "unqualified-cq-2"),
            (Prospect.QualificationStatus.NOT_A_FIT, "not-a-fit-cq"),
            (Prospect.QualificationStatus.ON_HOLD, "on-hold-cq"),
        ]:
            prospect = Prospect.objects.create(
                tenant=self.tenant,
                display_name=f"Prospect {label}",
                identity_key=label,
            )
            if status != Prospect.QualificationStatus.UNQUALIFIED:
                qualify_prospect(
                    tenant=self.tenant,
                    prospect=prospect,
                    qualification_status=status,
                    priority=Prospect.Priority.MEDIUM,
                    qualification_note="",
                    actor=self.user,
                )
            self._email_contact(prospect=prospect, email=f"{label}@example.com")
        items = self._queue()
        prospect_level = {
            item.prospect_id
            for item in items
            if item.category
            in {
                CommercialQueueCategory.OUTREACH_PENDING,
                CommercialQueueCategory.WAITING_OUTCOME,
                CommercialQueueCategory.MISSING_CONTACT,
                CommercialQueueCategory.READY_FOR_OUTREACH,
            }
        }
        self.assertEqual(prospect_level, set())

    def test_compact_projection_follow_up_dominates_prospect_level(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        qualify_prospect(
            tenant=self.tenant,
            prospect=self.qualified,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.HIGH,
            qualification_note="",
            actor=self.user,
        )
        contact = self._email_contact(email="dominate@example.com")
        draft = self._email_draft(contact, status=ProspectOutreachDraft.Status.READY)
        self._email_send(draft, status=ProspectOutreachSend.Status.SENT)
        self._follow_up(self.qualified, timezone.now() - timedelta(hours=1), idempotency_key="cq-dominate-overdue")
        items = self._queue(compact=True)
        self.assertIn(CommercialQueueCategory.FOLLOW_UP_OVERDUE, [item.category for item in items])
        self.assertNotIn(
            CommercialQueueCategory.WAITING_OUTCOME,
            self._categories_for_prospect(items, self.qualified.id),
        )

        today_prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Today Dominates",
            identity_key="today-dominates-cq",
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=today_prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        self._email_contact(prospect=today_prospect, email="todaydom@example.com")
        now = timezone.now()
        local_start = timezone.localtime(now).replace(hour=10, minute=0, second=0, microsecond=0)
        if timezone.is_naive(local_start):
            local_start = timezone.make_aware(local_start)
        self._follow_up(today_prospect, local_start + timedelta(hours=2), idempotency_key="cq-dominate-today")
        items_today = self._queue(now=local_start, compact=True)
        self.assertIn(CommercialQueueCategory.FOLLOW_UP_TODAY, [item.category for item in items_today])
        self.assertNotIn(
            CommercialQueueCategory.READY_FOR_OUTREACH,
            self._categories_for_prospect(items_today, today_prospect.id),
        )

    def test_prospect_never_in_two_c_f_categories_and_multiple_follow_ups(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        self._follow_up(self.qualified, timezone.now() - timedelta(days=1), idempotency_key="cq-multi-1")
        self._follow_up(self.qualified, timezone.now() - timedelta(hours=2), idempotency_key="cq-multi-2")
        items = self._queue()
        follow_items = [item for item in items if item.follow_up_id is not None]
        self.assertEqual(len(follow_items), 2)

        ready_prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Single Category",
            identity_key="single-category-cq",
        )
        qualify_prospect(
            tenant=self.tenant,
            prospect=ready_prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        self._email_contact(prospect=ready_prospect, email="single@example.com")
        items_ready = self._queue()
        cf_categories = [
            c
            for c in self._categories_for_prospect(items_ready, ready_prospect.id)
            if c
            in {
                CommercialQueueCategory.OUTREACH_PENDING,
                CommercialQueueCategory.WAITING_OUTCOME,
                CommercialQueueCategory.MISSING_CONTACT,
                CommercialQueueCategory.READY_FOR_OUTREACH,
            }
        ]
        self.assertEqual(len(cf_categories), 1)

    def test_missing_contact_suppressed_when_pipeline_exists(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory

        qualify_prospect(
            tenant=self.tenant,
            prospect=self.qualified,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.HIGH,
            qualification_note="",
            actor=self.user,
        )
        contact = self._email_contact(email="pipeline@example.com")
        contact.normalized_email = ""
        contact.email = ""
        contact.save()
        draft = create_outreach_draft(
            tenant=self.tenant,
            prospect=self.qualified,
            contact=contact,
            channel=ProspectOutreachDraft.Channel.OTHER,
            body="Tentativa registrada antes da limpeza do email.",
            actor=self.user,
        )
        items = self._queue()
        self.assertEqual(
            self._categories_for_prospect(items, self.qualified.id),
            [CommercialQueueCategory.OUTREACH_PENDING],
        )
        self.assertNotIn(CommercialQueueCategory.MISSING_CONTACT, self._categories_for_prospect(items, self.qualified.id))
        self.assertTrue(ProspectOutreachDraft.objects.filter(pk=draft.pk).exists())

    def test_counters_match_projection_and_deterministic_ordering(self):
        from prospecting.application.commercial_queue import CommercialQueueCategory, sort_commercial_queue_items

        low = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Zulu Clinic",
            identity_key="zulu-clinic-cq",
            priority=Prospect.Priority.LOW,
        )
        high = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Alpha Clinic",
            identity_key="alpha-clinic-cq",
            priority=Prospect.Priority.HIGH,
        )
        shared_qualified_at = timezone.now()
        for prospect in [low, high]:
            qualify_prospect(
                tenant=self.tenant,
                prospect=prospect,
                qualification_status=Prospect.QualificationStatus.QUALIFIED,
                priority=prospect.priority,
                qualification_note="",
                actor=self.user,
            )
            self._email_contact(prospect=prospect, email=f"{prospect.identity_key}@example.com")
        Prospect.objects.filter(pk__in=[low.pk, high.pk]).update(
            qualified_at=shared_qualified_at,
            updated_at=shared_qualified_at,
        )
        overdue_early = self._follow_up(
            self.qualified,
            timezone.now() - timedelta(days=2),
            idempotency_key="cq-order-overdue-early",
        )
        overdue_late = self._follow_up(
            self.qualified,
            timezone.now() - timedelta(hours=1),
            idempotency_key="cq-order-overdue-late",
        )
        items = self._queue(now=timezone.now())
        counters = self._counters(now=timezone.now())
        self.assertEqual(counters.total, len(items))
        self.assertEqual(counters.follow_up_overdue, sum(1 for i in items if i.category == CommercialQueueCategory.FOLLOW_UP_OVERDUE))
        self.assertEqual(counters.ready_for_outreach, sum(1 for i in items if i.category == CommercialQueueCategory.READY_FOR_OUTREACH))

        follow_order = [item.follow_up_id for item in items if item.follow_up_id is not None]
        self.assertEqual(follow_order.index(overdue_early.id), 0)
        self.assertLess(follow_order.index(overdue_early.id), follow_order.index(overdue_late.id))

        ready_items = [item for item in items if item.category == CommercialQueueCategory.READY_FOR_OUTREACH]
        self.assertEqual(ready_items[0].display_name, "Alpha Clinic")
        self.assertEqual(sort_commercial_queue_items(items), items)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="comercial@example.com",
    PROSPECTING_OUTREACH_EMAIL_ENABLED=True,
    PROSPECTING_OUTREACH_EMAIL_DRY_RUN=False,
)
class ProspectingOutreachSendIntegrationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator-send-int", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-send-int")
        self.prospect = Prospect.objects.create(tenant=self.tenant, display_name="Hospital ABC", identity_key="hospital-abc-send-int")
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        self.email_contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
            email="maria@hospital.example.com",
        )

    def test_django_email_backend_used_without_real_network(self):
        from django.core import mail

        draft = create_outreach_draft(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.email_contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            subject="Teste",
            body="Olá",
            status=ProspectOutreachDraft.Status.READY,
            actor=self.user,
        )
        send_outreach_draft_email(tenant=self.tenant, draft=draft, actor=self.user)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["maria@hospital.example.com"])


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
