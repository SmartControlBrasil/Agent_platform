from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from prospecting.application.prospects import promote_search_result_to_prospect
from prospecting.application.qualification import ACTION_PROSPECT_QUALIFICATION_UPDATED
from prospecting.application.search_runs import create_and_dispatch_search_run, synchronize_search_run
from prospecting.models import Prospect, ProspectEnrichment, ProspectSource, SearchResult, SearchRun, SearchRunExecutionAttempt
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
