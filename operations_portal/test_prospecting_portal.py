from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from datetime import timedelta

from django.utils import timezone

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from prospecting.application.prospects import promote_search_result_to_prospect
from prospecting.application.activities import ACTION_PROSPECT_ACTIVITY_CREATED, ACTION_PROSPECT_ACTIVITY_UPDATED
from prospecting.application.contacts import ACTION_PROSPECT_CONTACT_CREATED, ACTION_PROSPECT_CONTACT_UPDATED
from prospecting.application.outreach_drafts import (
    ACTION_OUTREACH_DRAFT_ARCHIVED,
    ACTION_OUTREACH_DRAFT_CREATED,
    ACTION_OUTREACH_DRAFT_READY,
    create_outreach_draft,
)
from prospecting.application.contact_outcomes import ACTION_CONTACT_OUTCOME_CREATED, record_prospect_contact_outcome
from prospecting.application.follow_ups import ACTION_FOLLOW_UP_COMPLETED, ACTION_FOLLOW_UP_CREATED
from prospecting.application.outreach_sends import ACTION_OUTREACH_SEND_SENT
from prospecting.application.qualification import ACTION_PROSPECT_QUALIFICATION_UPDATED, qualify_prospect
from prospecting.application.search_runs import create_and_dispatch_search_run, synchronize_search_run
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
from audit.models import AuditEvent
from tenants.models import Tenant, TenantMembership
from tools.application.lifecycle import claim_tool_execution, fail_tool_execution
from tools.models import AgentToolBinding, ToolDefinition, ToolExecution, ToolExecutor, ToolExecutorCapability


TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class ProspectingPortalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="scb-admin", password="pass")
        self.viewer = User.objects.create_user(username="scb-viewer", password="pass")
        self.other_admin = User.objects.create_user(username="other-admin", password="pass")

        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant")
        TenantMembership.objects.create(tenant=self.tenant, user=self.admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.viewer, role=TenantMembership.Role.VIEWER)
        TenantMembership.objects.create(tenant=self.other_tenant, user=self.other_admin, role=TenantMembership.Role.TENANT_ADMIN)

        self.project = Project.objects.create(tenant=self.tenant, name="Site institucional", slug="site")
        self.other_project = Project.objects.create(tenant=self.other_tenant, name="Outro", slug="outro")
        definition = AgentDefinition.objects.get(slug="prospecting")
        version = AgentVersion.objects.get(agent_definition=definition, version="1.0.0")
        self.installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=definition,
            agent_version=version,
            name="Prospecting A",
        )
        self.other_installation = AgentInstallation.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_definition=definition,
            agent_version=version,
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
            tool_definition=self.plan_tool,
            configuration={"max_queries": 4},
        )
        AgentToolBinding.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_installation=self.other_installation,
            tool_definition=self.maps_tool,
            configuration={},
        )
        self.run = SearchRun.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=self.installation,
            target_market="hospitais",
            target_region="São Paulo",
            objective="mapear contas",
            queries=["hospital privado são paulo"],
            max_results=10,
        )
        self.other_run = SearchRun.objects.create(
            tenant=self.other_tenant,
            project=self.other_project,
            agent_installation=self.other_installation,
            target_market="escolas",
            target_region="Curitiba",
            objective="mapear contas",
            queries=["escola curitiba"],
            max_results=10,
        )
        self.result = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=self.run,
            name="Hospital Exemplo",
            website="https://hospital.example.com",
            maps_url="https://maps.google.com/?cid=1",
            external_id="cid:1",
            source_query="hospital privado são paulo",
        )
        self.other_result = SearchResult.objects.create(
            tenant=self.other_tenant,
            search_run=self.other_run,
            name="Escola Exemplo",
        )

        self.client = Client()

    def _login(self, user):
        self.client.force_login(user)

    def test_authentication_required(self):
        response = self.client.get(reverse("operations_portal:prospecting_search_run_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_run_list_and_detail_are_tenant_scoped(self):
        self._login(self.admin)
        response = self.client.get(reverse("operations_portal:prospecting_search_run_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "São Paulo")
        self.assertNotContains(response, "Curitiba")

        detail = self.client.get(reverse("operations_portal:prospecting_search_run_detail", args=[self.run.id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Hospital Exemplo")

        forbidden = self.client.get(reverse("operations_portal:prospecting_search_run_detail", args=[self.other_run.id]))
        self.assertEqual(forbidden.status_code, 404)

    def test_promotion_is_post_only_and_idempotent(self):
        self._login(self.admin)
        get_attempt = self.client.get(reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]))
        self.assertEqual(get_attempt.status_code, 405)

        post = self.client.post(
            reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]),
            {"tenant": str(self.tenant.pk)},
        )
        self.assertEqual(post.status_code, 302)
        self.assertEqual(ProspectSource.objects.filter(search_result=self.result).count(), 1)

        post_again = self.client.post(
            reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]),
            {"tenant": str(self.tenant.pk)},
        )
        self.assertEqual(post_again.status_code, 302)
        self.assertEqual(ProspectSource.objects.filter(search_result=self.result).count(), 1)

    def test_viewer_cannot_promote_or_enrich(self):
        self._login(self.viewer)
        promote = self.client.post(
            reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]),
            {"tenant": str(self.tenant.pk)},
        )
        self.assertEqual(promote.status_code, 403)

        self._login(self.admin)
        self.client.post(
            reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]),
            {"tenant": str(self.tenant.pk)},
        )
        source = ProspectSource.objects.get(search_result=self.result)

        self._login(self.viewer)
        enrich = self.client.post(
            reverse("operations_portal:prospecting_add_enrichment", args=[source.prospect_id]),
            {"field": ProspectEnrichment.Field.EMAIL, "value": "contato@example.com", "source_type": ProspectEnrichment.SourceType.MANUAL},
        )
        self.assertEqual(enrich.status_code, 403)

    def test_prospect_list_and_detail_show_provenance_and_enrichment(self):
        self._login(self.admin)
        self.client.post(
            reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]),
            {"tenant": str(self.tenant.pk)},
        )
        source = ProspectSource.objects.get(search_result=self.result)

        add = self.client.post(
            reverse("operations_portal:prospecting_add_enrichment", args=[source.prospect_id]),
            {
                "field": ProspectEnrichment.Field.EMAIL,
                "value": " COMERCIAL@HOSPITAL.EXAMPLE.COM ",
                "source_type": ProspectEnrichment.SourceType.MANUAL,
                "source_reference": "planilha",
            },
        )
        self.assertEqual(add.status_code, 302)

        list_response = self.client.get(reverse("operations_portal:prospecting_prospect_list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "Hospital Exemplo")

        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[source.prospect_id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "SearchRun")
        self.assertContains(detail, "COMERCIAL@HOSPITAL.EXAMPLE.COM")

    def test_cross_tenant_promote_and_detail_are_blocked(self):
        self._login(self.admin)
        promote = self.client.post(
            reverse("operations_portal:prospecting_promote_search_result", args=[self.other_result.id]),
            {"tenant": str(self.tenant.pk)},
        )
        self.assertEqual(promote.status_code, 404)

        self._login(self.other_admin)
        self.client.post(
            reverse("operations_portal:prospecting_promote_search_result", args=[self.other_result.id]),
            {"tenant": str(self.other_tenant.pk)},
        )
        other_source = ProspectSource.objects.get(search_result=self.other_result)

        self._login(self.admin)
        forbidden = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[other_source.prospect_id]))
        self.assertEqual(forbidden.status_code, 404)

    def test_sidebar_contains_prospecting_entries(self):
        self._login(self.admin)
        response = self.client.get(reverse("operations_portal:prospecting_search_run_list"))
        self.assertContains(response, "Prospecção · Pesquisas")
        self.assertContains(response, "Prospecção · Prospects")
        self.assertContains(response, "Prospecção · Acompanhamentos")

    def test_new_search_button_visibility_depends_on_role(self):
        self._login(self.admin)
        admin_response = self.client.get(reverse("operations_portal:prospecting_search_run_list"))
        self.assertContains(admin_response, "+ Nova pesquisa")

        self._login(self.viewer)
        viewer_response = self.client.get(reverse("operations_portal:prospecting_search_run_list"))
        self.assertNotContains(viewer_response, "+ Nova pesquisa")

    def test_new_search_requires_specific_tenant_context(self):
        superuser = get_user_model().objects.create_superuser(username="root", password="pass", email="root@example.com")
        self._login(superuser)

        response = self.client.get(f"{reverse('operations_portal:prospecting_search_run_list')}?tenant=global")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Selecione um tenant para criar pesquisa")

    def test_new_search_create_flow_generates_plan_and_dispatches_run(self):
        self._login(self.admin)
        create_url = reverse("operations_portal:prospecting_search_run_create")
        execute_url = reverse("operations_portal:prospecting_search_run_execute")

        get_response = self.client.get(create_url)
        self.assertEqual(get_response.status_code, 200)

        plan_response = self.client.post(
            create_url,
            {
                "tenant": str(self.tenant.pk),
                "project": str(self.project.pk),
                "objective": "Encontrar hospitais para apresentar robôs",
                "target_region": "Barueri",
            },
        )
        self.assertEqual(plan_response.status_code, 200)
        self.assertContains(plan_response, "Queries sugeridas")

        session = self.client.session
        drafts = session.get("operations_portal_prospecting_drafts", {})
        self.assertEqual(len(drafts), 1)
        draft_id, draft = next(iter(drafts.items()))
        self.assertEqual(draft["project_id"], str(self.project.pk))
        self.assertTrue(draft["queries"])

        execute_response = self.client.post(
            execute_url,
            {
                "tenant": str(self.tenant.pk),
                "draft_id": draft_id,
                "selected_queries": draft["queries"][:2],
            },
        )
        self.assertEqual(execute_response.status_code, 302)
        run = SearchRun.objects.exclude(id=self.run.id).latest("created_at")
        self.assertEqual(run.tenant_id, self.tenant.id)
        self.assertEqual(run.project_id, self.project.id)
        self.assertEqual(run.agent_installation_id, self.installation.id)
        self.assertEqual(run.status, SearchRun.Status.DISPATCHED)
        self.assertEqual(run.objective, "Encontrar hospitais para apresentar robôs")
        self.assertEqual(run.target_region, "Barueri")
        self.assertEqual(len(run.queries), 2)
        self.assertEqual(str(run.id), execute_response.url.rstrip("/").split("/")[-1])

        retry_response = self.client.post(
            execute_url,
            {
                "tenant": str(self.tenant.pk),
                "draft_id": draft_id,
                "selected_queries": draft["queries"][:2],
            },
        )
        self.assertEqual(retry_response.status_code, 302)
        self.assertEqual(SearchRun.objects.filter(objective="Encontrar hospitais para apresentar robôs").count(), 1)

    def test_cross_tenant_project_is_rejected_on_new_search(self):
        self._login(self.admin)
        response = self.client.post(
            reverse("operations_portal:prospecting_search_run_create"),
            {
                "tenant": str(self.tenant.pk),
                "project": str(self.other_project.pk),
                "objective": "objetivo",
                "target_region": "região",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Revise os campos destacados")

    def test_viewer_cannot_open_new_search(self):
        self._login(self.viewer)
        response = self.client.get(reverse("operations_portal:prospecting_search_run_create"))
        self.assertEqual(response.status_code, 403)

    def test_new_search_execute_rejects_zero_queries(self):
        self._login(self.admin)
        create_url = reverse("operations_portal:prospecting_search_run_create")
        execute_url = reverse("operations_portal:prospecting_search_run_execute")
        self.client.post(
            create_url,
            {
                "tenant": str(self.tenant.pk),
                "project": str(self.project.pk),
                "objective": "Buscar hospitais",
                "target_region": "Barueri",
            },
        )
        draft_id = next(iter(self.client.session.get("operations_portal_prospecting_drafts", {}).keys()))

        response = self.client.post(
            execute_url,
            {
                "tenant": str(self.tenant.pk),
                "draft_id": draft_id,
                "selected_queries": [],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Selecione pelo menos uma query válida")

    def test_search_run_detail_shows_review_counters(self):
        self._login(self.admin)
        ignored = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=self.run,
            name="Hospital Ignorado",
            review_status=SearchResult.ReviewStatus.IGNORED,
        )
        promoted = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=self.run,
            name="Hospital Promovido",
        )
        self.client.post(
            reverse("operations_portal:prospecting_promote_search_result", args=[promoted.id]),
            {"tenant": str(self.tenant.pk)},
        )
        response = self.client.get(reverse("operations_portal:prospecting_search_run_detail", args=[self.run.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Resultados")
        self.assertContains(response, "Não revisados")
        self.assertContains(response, "Promovidos")
        self.assertContains(response, "Ignorados")
        self.assertContains(response, str(ignored.name))
        self.assertContains(response, str(promoted.name))

    def test_search_result_filters_and_pagination(self):
        self._login(self.admin)
        promoted = SearchResult.objects.create(tenant=self.tenant, search_run=self.run, name="Alpha Hospital", phone="119999999")
        self.client.post(
            reverse("operations_portal:prospecting_promote_search_result", args=[promoted.id]),
            {"tenant": str(self.tenant.pk)},
        )
        SearchResult.objects.create(
            tenant=self.tenant,
            search_run=self.run,
            name="Beta Clinic",
            review_status=SearchResult.ReviewStatus.IGNORED,
            website="https://beta.example.com",
        )
        for index in range(30):
            SearchResult.objects.create(
                tenant=self.tenant,
                search_run=self.run,
                name=f"Hospital Extra {index:02d}",
                phone="" if index % 2 else f"1100{index:02d}",
                website="" if index % 3 else f"https://site{index:02d}.example.com",
            )

        promoted_response = self.client.get(
            reverse("operations_portal:prospecting_search_run_detail", args=[self.run.id]),
            {"status": "promoted"},
        )
        self.assertContains(promoted_response, "Alpha Hospital")
        self.assertNotContains(promoted_response, "Beta Clinic")

        ignored_response = self.client.get(
            reverse("operations_portal:prospecting_search_run_detail", args=[self.run.id]),
            {"status": "ignored"},
        )
        self.assertContains(ignored_response, "Beta Clinic")
        self.assertNotContains(ignored_response, "Alpha Hospital")

        filtered_page = self.client.get(
            reverse("operations_portal:prospecting_search_run_detail", args=[self.run.id]),
            {"q": "Hospital Extra", "page": 2},
        )
        self.assertEqual(filtered_page.status_code, 200)
        self.assertContains(filtered_page, "page=1&q=Hospital+Extra")

    def test_view_only_can_filter_but_cannot_mutate(self):
        self._login(self.viewer)
        response = self.client.get(
            reverse("operations_portal:prospecting_search_run_detail", args=[self.run.id]),
            {"status": "unreviewed"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Promover selecionados")
        self.assertNotContains(response, "Promover para Prospect")

    def test_ignore_restore_and_bulk_actions(self):
        self._login(self.admin)
        first = SearchResult.objects.create(tenant=self.tenant, search_run=self.run, name="Hospital Lote 1")
        second = SearchResult.objects.create(tenant=self.tenant, search_run=self.run, name="Hospital Lote 2")

        ignore_response = self.client.post(
            reverse("operations_portal:prospecting_ignore_search_result", args=[first.id]),
            {"tenant": str(self.tenant.pk)},
        )
        self.assertEqual(ignore_response.status_code, 302)
        first.refresh_from_db()
        self.assertEqual(first.review_status, SearchResult.ReviewStatus.IGNORED)

        restore_response = self.client.post(
            reverse("operations_portal:prospecting_restore_search_result", args=[first.id]),
            {"tenant": str(self.tenant.pk)},
        )
        self.assertEqual(restore_response.status_code, 302)
        first.refresh_from_db()
        self.assertEqual(first.review_status, SearchResult.ReviewStatus.UNREVIEWED)

        bulk_ignore = self.client.post(
            reverse("operations_portal:prospecting_search_result_bulk_action", args=[self.run.id]),
            {
                "tenant": str(self.tenant.pk),
                "action": "ignore",
                "selected_result_ids": [str(first.id), str(second.id)],
            },
        )
        self.assertEqual(bulk_ignore.status_code, 302)
        self.assertEqual(
            SearchResult.objects.filter(
                id__in=[first.id, second.id],
                review_status=SearchResult.ReviewStatus.IGNORED,
            ).count(),
            2,
        )

        bulk_promote = self.client.post(
            reverse("operations_portal:prospecting_search_result_bulk_action", args=[self.run.id]),
            {
                "tenant": str(self.tenant.pk),
                "action": "promote",
                "selected_result_ids": [str(first.id), str(second.id)],
            },
        )
        self.assertEqual(bulk_promote.status_code, 302)
        self.assertEqual(ProspectSource.objects.filter(search_result__in=[first, second]).count(), 2)

    def test_bulk_actions_reject_empty_selection_and_cross_tenant_ids(self):
        self._login(self.admin)
        empty = self.client.post(
            reverse("operations_portal:prospecting_search_result_bulk_action", args=[self.run.id]),
            {"tenant": str(self.tenant.pk), "action": "ignore", "selected_result_ids": []},
        )
        self.assertEqual(empty.status_code, 302)
        self.assertEqual(SearchResult.objects.filter(review_status=SearchResult.ReviewStatus.IGNORED).count(), 0)

        cross = self.client.post(
            reverse("operations_portal:prospecting_search_result_bulk_action", args=[self.run.id]),
            {
                "tenant": str(self.tenant.pk),
                "action": "ignore",
                "selected_result_ids": [str(self.result.id), str(self.other_result.id)],
            },
        )
        self.assertEqual(cross.status_code, 302)
        self.result.refresh_from_db()
        self.assertEqual(self.result.review_status, SearchResult.ReviewStatus.UNREVIEWED)

    def _maps_executor(self, public_id):
        executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="SCB Chrome Executor Operacional",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id=public_id,
            last_seen_at=None,
        )
        ToolExecutorCapability.objects.create(executor=executor, tool_definition=self.maps_tool)
        return executor

    def _failed_operational_run(self):
        run = create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Diagnóstico operacional",
            target_region="Barueri",
            selected_queries=["hospitais em Barueri SP"],
        )
        execution = ToolExecution.objects.get(pk=run.agent_platform_execution_id)
        executor = self._maps_executor(f"portal-{execution.id}")
        claim_tool_execution(executor=executor, execution=execution)
        fail_tool_execution(executor=executor, execution=execution, error_code="results_not_loaded", error_message="feed vazio")
        return synchronize_search_run(search_run=run)

    def test_failed_run_shows_retry_and_human_error(self):
        run = self._failed_operational_run()
        self._login(self.admin)
        response = self.client.get(reverse("operations_portal:prospecting_search_run_detail", args=[run.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tentar novamente")
        self.assertContains(response, "Os resultados do Google Maps não ficaram disponíveis a tempo.")
        self.assertContains(response, "Histórico de execução")
        self.assertContains(response, "Tentativa 1")
        self.assertNotContains(response, "aep_")
        self.assertNotContains(response, "Authorization")

    def test_completed_and_running_do_not_show_retry(self):
        self._login(self.admin)
        completed = create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Concluída",
            target_region="Barueri",
            selected_queries=["hospitais Barueri"],
        )
        completed.status = SearchRun.Status.COMPLETED
        completed.save(update_fields=["status"])
        completed_response = self.client.get(reverse("operations_portal:prospecting_search_run_detail", args=[completed.id]))
        self.assertNotContains(completed_response, "Tentar novamente")

        running = create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Em execução",
            target_region="Barueri",
            selected_queries=["hospitais Barueri"],
        )
        execution = ToolExecution.objects.get(pk=running.agent_platform_execution_id)
        claim_tool_execution(executor=self._maps_executor(f"running-{execution.id}"), execution=execution)
        running = synchronize_search_run(search_run=running)
        running_response = self.client.get(reverse("operations_portal:prospecting_search_run_detail", args=[running.id]))
        self.assertNotContains(running_response, "Tentar novamente")
        self.assertNotContains(running_response, "Cancelar pesquisa")

    def test_retry_is_post_only_and_manager_only(self):
        run = self._failed_operational_run()
        url = reverse("operations_portal:prospecting_search_run_retry", args=[run.id])
        self._login(self.viewer)
        viewer = self.client.post(url, {"tenant": str(self.tenant.pk)})
        self.assertEqual(viewer.status_code, 403)
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=run).count(), 1)

        self._login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 405)
        retry = self.client.post(url, {"tenant": str(self.tenant.pk)})
        self.assertEqual(retry.status_code, 302)
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=run).count(), 2)
        run.refresh_from_db()
        self.assertEqual(run.status, SearchRun.Status.DISPATCHED)

    def test_dispatched_offline_executor_warning_and_cancel(self):
        run = create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Aguardando",
            target_region="Barueri",
            selected_queries=["hospitais Barueri"],
        )
        self._maps_executor("offline-scb")
        self._login(self.admin)
        response = self.client.get(reverse("operations_portal:prospecting_search_run_detail", args=[run.id]))
        self.assertContains(response, "Aguardando executor")
        self.assertContains(response, "Nenhum executor compatível está online no momento.")
        self.assertContains(response, "Cancelar pesquisa")
        self.assertContains(response, "SCB Chrome Executor Operacional")

        cancel_get = self.client.get(reverse("operations_portal:prospecting_search_run_cancel", args=[run.id]))
        self.assertEqual(cancel_get.status_code, 405)
        cancel = self.client.post(
            reverse("operations_portal:prospecting_search_run_cancel", args=[run.id]),
            {"tenant": str(self.tenant.pk)},
        )
        self.assertEqual(cancel.status_code, 302)
        run.refresh_from_db()
        self.assertEqual(run.status, SearchRun.Status.CANCELLED)
        self.assertTrue(SearchResult.objects.filter(search_run=self.run).exists())

    def test_cross_tenant_retry_and_cancel_are_rejected(self):
        run = self._failed_operational_run()
        self._login(self.other_admin)
        retry = self.client.post(
            reverse("operations_portal:prospecting_search_run_retry", args=[run.id]),
            {"tenant": str(self.other_tenant.pk)},
        )
        cancel = self.client.post(
            reverse("operations_portal:prospecting_search_run_cancel", args=[run.id]),
            {"tenant": str(self.other_tenant.pk)},
        )
        self.assertEqual(retry.status_code, 404)
        self.assertEqual(cancel.status_code, 404)
        self.assertEqual(SearchRunExecutionAttempt.objects.filter(search_run=run).count(), 1)

    def test_online_executor_does_not_show_offline_warning(self):
        run = create_and_dispatch_search_run(
            tenant=self.tenant,
            project=self.project,
            objective="Online",
            target_region="Barueri",
            selected_queries=["hospitais Barueri"],
        )
        executor = self._maps_executor("online-scb")
        executor.last_seen_at = timezone.now()
        executor.save(update_fields=["last_seen_at", "updated_at"])
        self._login(self.admin)
        response = self.client.get(reverse("operations_portal:prospecting_search_run_detail", args=[run.id]))
        self.assertContains(response, "Online")
        self.assertNotContains(response, "Nenhum executor compatível está online no momento.")


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class ProspectingQualificationPortalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="scb-admin-q", password="pass")
        self.viewer = User.objects.create_user(username="scb-viewer-q", password="pass")
        self.other_admin = User.objects.create_user(username="other-admin-q", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-q")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant-q")
        TenantMembership.objects.create(tenant=self.tenant, user=self.admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.viewer, role=TenantMembership.Role.VIEWER)
        TenantMembership.objects.create(tenant=self.other_tenant, user=self.other_admin, role=TenantMembership.Role.TENANT_ADMIN)
        self.project = Project.objects.create(tenant=self.tenant, name="Comercial", slug="comercial-q")
        definition = AgentDefinition.objects.get(slug="prospecting")
        version = AgentVersion.objects.get(agent_definition=definition, version="1.0.0")
        installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=definition,
            agent_version=version,
            name="Prospecting Q",
        )
        run = SearchRun.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=installation,
            target_region="São Paulo",
            queries=["hospital privado"],
            max_results=10,
        )
        result = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=run,
            name="Hospital Qualificação",
            external_id="cid-q-1",
            source_query="hospital privado",
        )
        self.prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=result)
        self.client = Client()

    def _qualify_payload(self, **overrides):
        payload = {
            "qualification_status": Prospect.QualificationStatus.QUALIFIED,
            "priority": Prospect.Priority.HIGH,
            "qualification_note": "Canal comercial público.",
        }
        payload.update(overrides)
        return payload

    def test_detail_shows_qualification_block_and_viewer_cannot_post(self):
        self.client.force_login(self.viewer)
        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Qualificação")
        self.assertContains(detail, "Não qualificado")
        self.assertNotContains(detail, "Salvar qualificação")

        post = self.client.post(
            reverse("operations_portal:prospecting_qualify_prospect", args=[self.prospect.id]),
            self._qualify_payload(),
        )
        self.assertEqual(post.status_code, 403)
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.qualification_status, Prospect.QualificationStatus.UNQUALIFIED)

    def test_admin_post_updates_qualification_and_get_is_read_only(self):
        self.client.force_login(self.admin)
        url = reverse("operations_portal:prospecting_qualify_prospect", args=[self.prospect.id])
        self.assertEqual(self.client.get(url).status_code, 405)

        response = self.client.post(url, self._qualify_payload())
        self.assertEqual(response.status_code, 302)
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.qualification_status, Prospect.QualificationStatus.QUALIFIED)
        self.assertEqual(self.prospect.priority, Prospect.Priority.HIGH)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_QUALIFICATION_UPDATED).exists())

        list_response = self.client.get(
            reverse("operations_portal:prospecting_prospect_list"),
            {"qualification_status": Prospect.QualificationStatus.QUALIFIED},
        )
        self.assertContains(list_response, "Hospital Qualificação")
        self.assertContains(list_response, "Qualificado")
        self.assertContains(list_response, "Alta")

    def test_priority_filter_and_cross_tenant_protection(self):
        self.client.force_login(self.admin)
        self.client.post(
            reverse("operations_portal:prospecting_qualify_prospect", args=[self.prospect.id]),
            self._qualify_payload(),
        )
        filtered = self.client.get(
            reverse("operations_portal:prospecting_prospect_list"),
            {"priority": Prospect.Priority.HIGH},
        )
        self.assertContains(filtered, "Hospital Qualificação")

        other_run = SearchRun.objects.create(
            tenant=self.other_tenant,
            project=Project.objects.create(tenant=self.other_tenant, name="Outro", slug="outro-proj-q"),
            target_region="Curitiba",
            queries=["escola"],
            max_results=5,
        )
        other_result = SearchResult.objects.create(
            tenant=self.other_tenant,
            search_run=other_run,
            name="Escola Outra",
            external_id="cid-other",
            source_query="escola",
        )
        other_prospect = promote_search_result_to_prospect(tenant=self.other_tenant, search_result=other_result)
        forbidden = self.client.post(
            reverse("operations_portal:prospecting_qualify_prospect", args=[other_prospect.id]),
            self._qualify_payload(),
        )
        self.assertEqual(forbidden.status_code, 404)


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class ProspectingContactPortalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="scb-admin-contact", password="pass")
        self.viewer = User.objects.create_user(username="scb-viewer-contact", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-contact")
        TenantMembership.objects.create(tenant=self.tenant, user=self.admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.viewer, role=TenantMembership.Role.VIEWER)
        self.project = Project.objects.create(tenant=self.tenant, name="Comercial", slug="comercial-contact")
        definition = AgentDefinition.objects.get(slug="prospecting")
        version = AgentVersion.objects.get(agent_definition=definition, version="1.0.0")
        installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=definition,
            agent_version=version,
            name="Prospecting Contact Portal",
        )
        run = SearchRun.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=installation,
            target_region="São Paulo",
            queries=["hospital privado"],
            max_results=10,
        )
        result = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=run,
            name="Hospital Contatos",
            external_id="cid-contact-1",
            source_query="hospital privado",
        )
        self.prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=result)
        self.client = Client()

    def _contact_payload(self, **overrides):
        payload = {
            "name": "Maria Silva",
            "role_title": "Compras",
            "email": "maria@hospital.example.com",
            "phone": "(11) 99999-0000",
            "note": "Canal comercial.",
        }
        payload.update(overrides)
        return payload

    def test_viewer_sees_contacts_but_cannot_create(self):
        self.client.force_login(self.viewer)
        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Contatos")
        self.assertNotContains(detail, "+ Adicionar contato")
        create = self.client.post(
            reverse("operations_portal:prospecting_create_prospect_contact", args=[self.prospect.id]),
            self._contact_payload(),
        )
        self.assertEqual(create.status_code, 403)

    def test_manager_creates_edits_and_lists_contact_count(self):
        self.client.force_login(self.admin)
        create_url = reverse("operations_portal:prospecting_create_prospect_contact", args=[self.prospect.id])
        self.assertEqual(self.client.get(create_url).status_code, 405)
        response = self.client.post(create_url, self._contact_payload())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_CONTACT_CREATED).exists())

        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Maria Silva")
        self.assertContains(detail, "maria@hospital.example.com")

        contact = self.prospect.contacts.get()
        edit = self.client.post(
            reverse("operations_portal:prospecting_update_prospect_contact", args=[self.prospect.id, contact.id]),
            self._contact_payload(role_title="Compras e suprimentos"),
        )
        self.assertEqual(edit.status_code, 302)
        contact.refresh_from_db()
        self.assertEqual(contact.role_title, "Compras e suprimentos")
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_CONTACT_UPDATED).exists())

        listing = self.client.get(reverse("operations_portal:prospecting_prospect_list"))
        self.assertContains(listing, "Hospital Contatos")
        self.assertContains(listing, ">1<")

    def test_invalid_post_and_cross_tenant_delete(self):
        self.client.force_login(self.admin)
        bad = self.client.post(
            reverse("operations_portal:prospecting_create_prospect_contact", args=[self.prospect.id]),
            {"name": "", "email": "", "phone": ""},
        )
        self.assertEqual(bad.status_code, 302)
        self.assertEqual(self.prospect.contacts.count(), 0)

        self.client.post(
            reverse("operations_portal:prospecting_create_prospect_contact", args=[self.prospect.id]),
            self._contact_payload(),
        )
        contact = self.prospect.contacts.get()

        other_tenant = Tenant.objects.create(name="Outro", slug="outro-contact-x")
        other_prospect = Prospect.objects.create(tenant=other_tenant, display_name="Outro", identity_key="outro-x")
        forbidden = self.client.post(
            reverse("operations_portal:prospecting_delete_prospect_contact", args=[other_prospect.id, contact.id]),
        )
        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(self.prospect.contacts.count(), 1)


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class ProspectingActivityPortalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="scb-admin-act", password="pass")
        self.viewer = User.objects.create_user(username="scb-viewer-act", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-act")
        TenantMembership.objects.create(tenant=self.tenant, user=self.admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.viewer, role=TenantMembership.Role.VIEWER)
        self.project = Project.objects.create(tenant=self.tenant, name="Comercial", slug="comercial-act")
        definition = AgentDefinition.objects.get(slug="prospecting")
        version = AgentVersion.objects.get(agent_definition=definition, version="1.0.0")
        installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=definition,
            agent_version=version,
            name="Prospecting Activity Portal",
        )
        run = SearchRun.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=installation,
            target_region="São Paulo",
            queries=["hospital privado"],
            max_results=10,
        )
        result = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=run,
            name="Hospital Atividades",
            external_id="cid-act-1",
            source_query="hospital privado",
        )
        self.prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=result)
        self.contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
            email="maria@hospital.example.com",
        )
        self.client = Client()

    def _activity_payload(self, **overrides):
        payload = {
            "activity_type": ProspectActivity.ActivityType.CALL,
            "contact": str(self.contact.pk),
            "occurred_at": timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M"),
            "note": "Apresentação inicial realizada.",
        }
        payload.update(overrides)
        return payload

    def test_viewer_sees_timeline_but_cannot_create(self):
        self.client.force_login(self.viewer)
        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Histórico comercial")
        self.assertNotContains(detail, "+ Registrar atividade")
        create = self.client.post(
            reverse("operations_portal:prospecting_create_prospect_activity", args=[self.prospect.id]),
            self._activity_payload(),
        )
        self.assertEqual(create.status_code, 403)

    def test_manager_creates_edits_and_lists_last_activity(self):
        self.client.force_login(self.admin)
        create_url = reverse("operations_portal:prospecting_create_prospect_activity", args=[self.prospect.id])
        self.assertEqual(self.client.get(create_url).status_code, 405)
        response = self.client.post(create_url, self._activity_payload())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_ACTIVITY_CREATED).exists())

        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Ligação")
        self.assertContains(detail, "Maria Silva")
        self.assertContains(detail, "Apresentação inicial realizada.")

        activity = self.prospect.activities.get()
        edit = self.client.post(
            reverse("operations_portal:prospecting_update_prospect_activity", args=[self.prospect.id, activity.id]),
            self._activity_payload(note="Apresentação inicial — pediu material."),
        )
        self.assertEqual(edit.status_code, 302)
        activity.refresh_from_db()
        self.assertIn("pediu material", activity.note)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_PROSPECT_ACTIVITY_UPDATED).exists())

        listing = self.client.get(reverse("operations_portal:prospecting_prospect_list"))
        self.assertContains(listing, "Hospital Atividades")
        self.assertContains(listing, ">1<")

    def test_invalid_post_and_cross_tenant_delete(self):
        self.client.force_login(self.admin)
        bad = self.client.post(
            reverse("operations_portal:prospecting_create_prospect_activity", args=[self.prospect.id]),
            {"activity_type": ProspectActivity.ActivityType.NOTE, "note": "", "occurred_at": "2026-09-25T10:00"},
        )
        self.assertEqual(bad.status_code, 302)
        self.assertEqual(self.prospect.activities.count(), 0)

        self.client.post(
            reverse("operations_portal:prospecting_create_prospect_activity", args=[self.prospect.id]),
            self._activity_payload(),
        )
        activity = self.prospect.activities.get()
        other_tenant = Tenant.objects.create(name="Outro", slug="outro-act-x")
        other_prospect = Prospect.objects.create(tenant=other_tenant, display_name="Outro", identity_key="outro-act-x")
        forbidden = self.client.post(
            reverse("operations_portal:prospecting_delete_prospect_activity", args=[other_prospect.id, activity.id]),
        )
        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(self.prospect.activities.count(), 1)


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class ProspectingOutreachDraftPortalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="scb-admin-outreach", password="pass")
        self.viewer = User.objects.create_user(username="scb-viewer-outreach", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-outreach")
        TenantMembership.objects.create(tenant=self.tenant, user=self.admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.viewer, role=TenantMembership.Role.VIEWER)
        self.project = Project.objects.create(tenant=self.tenant, name="Comercial", slug="comercial-outreach")
        definition = AgentDefinition.objects.get(slug="prospecting")
        version = AgentVersion.objects.get(agent_definition=definition, version="1.0.0")
        installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=definition,
            agent_version=version,
            name="Prospecting Outreach Portal",
        )
        run = SearchRun.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=installation,
            target_region="São Paulo",
            queries=["hospital privado"],
            max_results=10,
        )
        result = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=run,
            name="Hospital Abordagens",
            external_id="cid-out-1",
            source_query="hospital privado",
        )
        self.prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=result)
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.admin,
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
            phone="11988887777",
        )
        self.client = Client()

    def _draft_payload(self, **overrides):
        payload = {
            "contact": str(self.email_contact.pk),
            "channel": ProspectOutreachDraft.Channel.EMAIL,
            "subject": "Apresentação de soluções robóticas",
            "body": "Prezada Maria, gostaríamos de apresentar nossas soluções.",
        }
        payload.update(overrides)
        return payload

    def test_viewer_sees_outreach_section_but_cannot_create(self):
        self.client.force_login(self.viewer)
        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Abordagens")
        self.assertNotContains(detail, "+ Nova abordagem")
        create = self.client.post(
            reverse("operations_portal:prospecting_create_outreach_draft", args=[self.prospect.id]),
            self._draft_payload(),
        )
        self.assertEqual(create.status_code, 403)
        self.assertEqual(self.prospect.outreach_drafts.count(), 0)

    def test_manager_creates_edits_ready_and_archives(self):
        self.client.force_login(self.admin)
        create_url = reverse("operations_portal:prospecting_create_outreach_draft", args=[self.prospect.id])
        self.assertEqual(self.client.get(create_url).status_code, 405)
        response = self.client.post(create_url, self._draft_payload())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_DRAFT_CREATED).exists())
        self.assertEqual(self.prospect.activities.count(), 0)

        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Email")
        self.assertContains(detail, "Maria Silva")
        self.assertContains(detail, "Apresentação de soluções robóticas")
        self.assertNotContains(detail, "Enviar")

        draft = self.prospect.outreach_drafts.get()
        edit = self.client.post(
            reverse("operations_portal:prospecting_update_outreach_draft", args=[self.prospect.id, draft.id]),
            self._draft_payload(body="Prezada Maria, seguimos à disposição."),
        )
        self.assertEqual(edit.status_code, 302)
        draft.refresh_from_db()
        self.assertIn("seguimos", draft.body)

        ready = self.client.post(
            reverse("operations_portal:prospecting_mark_outreach_draft_ready", args=[self.prospect.id, draft.id]),
        )
        self.assertEqual(ready.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, ProspectOutreachDraft.Status.READY)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_DRAFT_READY).exists())

        archived = self.client.post(
            reverse("operations_portal:prospecting_archive_outreach_draft", args=[self.prospect.id, draft.id]),
        )
        self.assertEqual(archived.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, ProspectOutreachDraft.Status.ARCHIVED)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_DRAFT_ARCHIVED).exists())

    def test_channel_without_destination_and_cross_tenant(self):
        self.client.force_login(self.admin)
        bad = self.client.post(
            reverse("operations_portal:prospecting_create_outreach_draft", args=[self.prospect.id]),
            self._draft_payload(contact=str(self.phone_contact.pk), channel=ProspectOutreachDraft.Channel.EMAIL),
        )
        self.assertEqual(bad.status_code, 302)
        self.assertEqual(self.prospect.outreach_drafts.count(), 0)

        create_url = reverse("operations_portal:prospecting_create_outreach_draft", args=[self.prospect.id])
        self.client.post(create_url, self._draft_payload())
        draft = self.prospect.outreach_drafts.get()
        other_tenant = Tenant.objects.create(name="Outro", slug="outro-out-x")
        other_prospect = Prospect.objects.create(tenant=other_tenant, display_name="Outro", identity_key="outro-out-x")
        forbidden = self.client.post(
            reverse("operations_portal:prospecting_archive_outreach_draft", args=[other_prospect.id, draft.id]),
        )
        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(self.prospect.outreach_drafts.count(), 1)


@override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES=TEST_STORAGES,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="comercial@example.com",
    PROSPECTING_OUTREACH_EMAIL_ENABLED=True,
    PROSPECTING_OUTREACH_EMAIL_DRY_RUN=False,
)
class ProspectingOutreachEmailPortalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="scb-admin-send", password="pass")
        self.viewer = User.objects.create_user(username="scb-viewer-send", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-send-portal")
        TenantMembership.objects.create(tenant=self.tenant, user=self.admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.viewer, role=TenantMembership.Role.VIEWER)
        self.project = Project.objects.create(tenant=self.tenant, name="Comercial", slug="comercial-send")
        definition = AgentDefinition.objects.get(slug="prospecting")
        version = AgentVersion.objects.get(agent_definition=definition, version="1.0.0")
        installation = AgentInstallation.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_definition=definition,
            agent_version=version,
            name="Prospecting Send Portal",
        )
        run = SearchRun.objects.create(
            tenant=self.tenant,
            project=self.project,
            agent_installation=installation,
            target_region="São Paulo",
            queries=["hospital privado"],
            max_results=10,
        )
        result = SearchResult.objects.create(
            tenant=self.tenant,
            search_run=run,
            name="Hospital Envio",
            external_id="cid-send-1",
            source_query="hospital privado",
        )
        self.prospect = promote_search_result_to_prospect(tenant=self.tenant, search_result=result)
        qualify_prospect(
            tenant=self.tenant,
            prospect=self.prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.admin,
        )
        self.email_contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
            email="maria@hospital.example.com",
        )
        self.ready_draft = create_outreach_draft(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.email_contact,
            channel=ProspectOutreachDraft.Channel.EMAIL,
            subject="Apresentação comercial",
            body="Prezada Maria, teste.",
            status=ProspectOutreachDraft.Status.READY,
            actor=self.admin,
        )
        self.client = Client()

    def test_ready_shows_send_button_draft_does_not(self):
        self.client.force_login(self.admin)
        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Enviar e-mail")
        self.ready_draft.status = ProspectOutreachDraft.Status.DRAFT
        self.ready_draft.save(update_fields=["status"])
        detail_draft = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertNotContains(detail_draft, "Enviar e-mail")

    def test_viewer_cannot_send_manager_can(self):
        confirm_url = reverse(
            "operations_portal:prospecting_outreach_email_send_confirm",
            args=[self.prospect.id, self.ready_draft.id],
        )
        execute_url = reverse(
            "operations_portal:prospecting_outreach_email_send_execute",
            args=[self.prospect.id, self.ready_draft.id],
        )
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(execute_url).status_code, 403)
        self.client.force_login(self.admin)
        confirm = self.client.get(confirm_url)
        self.assertContains(confirm, "Envio real de e-mail")
        self.assertContains(confirm, "maria@hospital.example.com")
        self.assertEqual(self.client.get(execute_url).status_code, 405)
        from django.core import mail

        response = self.client.post(execute_url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_OUTREACH_SEND_SENT).exists())
        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Enviado")
        self.assertNotContains(detail, ">Enviar e-mail<")

    def test_double_post_does_not_duplicate_email(self):
        from django.core import mail

        execute_url = reverse(
            "operations_portal:prospecting_outreach_email_send_execute",
            args=[self.prospect.id, self.ready_draft.id],
        )
        self.client.force_login(self.admin)
        self.client.post(execute_url)
        self.client.post(execute_url)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(ProspectOutreachSend.objects.filter(draft=self.ready_draft, status=ProspectOutreachSend.Status.SENT).count(), 1)


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class ProspectingFollowUpPortalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="scb-admin-follow", password="pass")
        self.viewer = User.objects.create_user(username="scb-viewer-follow", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-follow-portal")
        TenantMembership.objects.create(tenant=self.tenant, user=self.admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.viewer, role=TenantMembership.Role.VIEWER)
        self.prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Hospital Follow",
            identity_key="hospital-follow-portal",
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
        )
        self.contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
            email="maria@hospital.example.com",
        )
        self.client = Client()

    def test_viewer_read_only_manager_records_outcome_and_follow_up(self):
        self.client.force_login(self.viewer)
        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Próximas ações")
        self.assertNotContains(detail, "Salvar resultado")
        forbidden = self.client.post(
            reverse("operations_portal:prospecting_record_contact_outcome", args=[self.prospect.id]),
            {},
        )
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.admin)
        due = (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        payload = {
            "idempotency_key": "portal-outcome-1",
            "follow_up_idempotency_key": "portal-follow-1",
            "contact": str(self.contact.pk),
            "outcome": ProspectContactOutcome.Outcome.CALLBACK_REQUESTED,
            "occurred_at": timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M"),
            "note": "Pediu retorno na terça.",
            "create_follow_up": "on",
            "follow_up_action_type": ProspectFollowUp.ActionType.CALL,
            "follow_up_due_at": due,
            "follow_up_note": "Ligar após reunião.",
        }
        response = self.client.post(reverse("operations_portal:prospecting_record_contact_outcome", args=[self.prospect.id]), payload)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_CONTACT_OUTCOME_CREATED).exists())
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_FOLLOW_UP_CREATED).exists())
        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Pediu retorno")
        self.assertContains(detail, "Ligar após reunião")

        follow_up = self.prospect.follow_ups.get()
        complete = self.client.post(
            reverse("operations_portal:prospecting_complete_follow_up", args=[self.prospect.id, follow_up.id]),
        )
        self.assertEqual(complete.status_code, 302)
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, ProspectFollowUp.Status.COMPLETED)
        self.assertTrue(AuditEvent.objects.filter(action=ACTION_FOLLOW_UP_COMPLETED).exists())

    def test_standalone_follow_up_and_overdue_badge(self):
        self.client.force_login(self.admin)
        past = (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        self.client.post(
            reverse("operations_portal:prospecting_create_follow_up", args=[self.prospect.id]),
            {
                "idempotency_key": "portal-follow-2",
                "action_type": ProspectFollowUp.ActionType.REVIEW,
                "due_at": past,
                "contact": str(self.contact.pk),
                "note": "Revisar proposta",
            },
        )
        detail = self.client.get(reverse("operations_portal:prospecting_prospect_detail", args=[self.prospect.id]))
        self.assertContains(detail, "Atrasado")
        listing = self.client.get(reverse("operations_portal:prospecting_prospect_list") + "?follow_up=overdue")
        self.assertContains(listing, "Hospital Follow")


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class ProspectingFollowUpQueuePortalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="scb-admin-queue", password="pass")
        self.viewer = User.objects.create_user(username="scb-viewer-queue", password="pass")
        self.other_admin = User.objects.create_user(username="other-admin-queue", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-queue-portal")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant-queue-portal")
        TenantMembership.objects.create(tenant=self.tenant, user=self.admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.viewer, role=TenantMembership.Role.VIEWER)
        TenantMembership.objects.create(tenant=self.other_tenant, user=self.other_admin, role=TenantMembership.Role.TENANT_ADMIN)
        self.prospect = Prospect.objects.create(
            tenant=self.tenant,
            display_name="Hospital Queue",
            identity_key="hospital-queue-portal",
            priority=Prospect.Priority.HIGH,
        )
        self.other_prospect = Prospect.objects.create(
            tenant=self.other_tenant,
            display_name="Outro Hospital",
            identity_key="outro-hospital-queue",
        )
        self.contact = ProspectContact.objects.create(
            tenant=self.tenant,
            prospect=self.prospect,
            name="Maria Silva",
        )
        self.client = Client()
        self.queue_url = reverse("operations_portal:prospecting_follow_up_queue")

    def _create_follow_up(self, *, due_at, action_type=ProspectFollowUp.ActionType.CALL, note="Ligar", key=None, tenant=None, prospect=None):
        from prospecting.application.follow_ups import create_prospect_follow_up

        tenant = tenant or self.tenant
        prospect = prospect or self.prospect
        follow_up, _ = create_prospect_follow_up(
            tenant=tenant,
            prospect=prospect,
            action_type=action_type,
            due_at=due_at,
            contact=self.contact if prospect == self.prospect else None,
            note=note,
            idempotency_key=key or uuid.uuid4().hex,
            actor=self.admin,
        )
        return follow_up

    def test_route_viewer_and_manager_access(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.queue_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Acompanhamentos")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.queue_url).status_code, 200)

    def test_viewer_cannot_complete_or_cancel(self):
        follow_up = self._create_follow_up(due_at=timezone.now() + timedelta(days=1))
        self.client.force_login(self.viewer)
        page = self.client.get(self.queue_url)
        self.assertNotContains(page, "Concluir")
        complete = self.client.post(reverse("operations_portal:prospecting_follow_up_queue_complete", args=[follow_up.id]))
        self.assertEqual(complete.status_code, 403)
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, ProspectFollowUp.Status.PENDING)

    def test_manager_complete_and_cancel_from_queue(self):
        follow_up = self._create_follow_up(due_at=timezone.now() + timedelta(days=1), key="queue-complete")
        self.client.force_login(self.admin)
        complete = self.client.post(
            reverse("operations_portal:prospecting_follow_up_queue_complete", args=[follow_up.id]),
            {"queue_querystring": "situation=pending"},
        )
        self.assertEqual(complete.status_code, 302)
        self.assertIn("situation=pending", complete.url)
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, ProspectFollowUp.Status.COMPLETED)

        cancel_target = self._create_follow_up(due_at=timezone.now() + timedelta(days=2), key="queue-cancel")
        cancel = self.client.post(reverse("operations_portal:prospecting_follow_up_queue_cancel", args=[cancel_target.id]))
        self.assertEqual(cancel.status_code, 302)
        cancel_target.refresh_from_db()
        self.assertEqual(cancel_target.status, ProspectFollowUp.Status.CANCELLED)

    def test_cross_tenant_post_is_blocked(self):
        other_follow_up = self._create_follow_up(
            due_at=timezone.now() + timedelta(days=1),
            tenant=self.other_tenant,
            prospect=self.other_prospect,
            key="other-follow",
        )
        self.client.force_login(self.admin)
        response = self.client.post(reverse("operations_portal:prospecting_follow_up_queue_complete", args=[other_follow_up.id]))
        self.assertEqual(response.status_code, 404)

    def test_get_does_not_mutate_status(self):
        follow_up = self._create_follow_up(due_at=timezone.now() + timedelta(days=1), key="queue-get")
        self.client.force_login(self.admin)
        self.client.get(self.queue_url)
        self.client.get(self.queue_url + "?situation=overdue")
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, ProspectFollowUp.Status.PENDING)

    def test_filters_counters_and_empty_states(self):
        now = timezone.now()
        self._create_follow_up(due_at=now - timedelta(days=1), key="f-overdue", note="Atrasado item")
        self._create_follow_up(
            due_at=now + timedelta(days=5),
            action_type=ProspectFollowUp.ActionType.EMAIL,
            key="f-future",
            note="Futuro item",
        )
        self.client.force_login(self.admin)
        page = self.client.get(self.queue_url)
        self.assertContains(page, "Atrasados")
        self.assertContains(page, "Hospital Queue")
        overdue = self.client.get(self.queue_url + "?situation=overdue")
        self.assertContains(overdue, "Atrasado item")
        self.assertNotContains(overdue, "Futuro item")
        empty_today = self.client.get(self.queue_url + "?situation=today")
        self.assertContains(empty_today, "Nenhum acompanhamento para hoje.")
        typed = self.client.get(self.queue_url + "?action_type=EMAIL")
        self.assertContains(typed, "Futuro item")
        self.assertNotContains(typed, "Atrasado item")
        search = self.client.get(self.queue_url + "?prospect_q=Hospital")
        self.assertContains(search, "Hospital Queue")
        priority = self.client.get(self.queue_url + "?priority=HIGH")
        self.assertContains(priority, "Hospital Queue")

    def test_latest_outcome_on_queue_page(self):
        self.client.force_login(self.admin)
        record_prospect_contact_outcome(
            tenant=self.tenant,
            prospect=self.prospect,
            contact=self.contact,
            outcome=ProspectContactOutcome.Outcome.CALLBACK_REQUESTED,
            idempotency_key="queue-portal-outcome",
            actor=self.admin,
        )
        self._create_follow_up(due_at=timezone.now() + timedelta(days=1), key="with-outcome")
        response = self.client.get(self.queue_url)
        self.assertContains(response, "Pediu retorno")

    def test_global_read_only_hides_mutations(self):
        superuser = get_user_model().objects.create_superuser(username="root-queue", password="pass", email="root@example.com")
        self._create_follow_up(due_at=timezone.now() + timedelta(days=1), key="global-ro")
        self.client.force_login(superuser)
        response = self.client.get(f"{self.queue_url}?tenant=global")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visão agregada")
        self.assertNotContains(response, "Concluir")
