from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from prospecting.models import ProspectEnrichment, ProspectSource, SearchResult, SearchRun
from tenants.models import Tenant, TenantMembership
from tools.models import AgentToolBinding, ToolDefinition


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

        post = self.client.post(reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]))
        self.assertEqual(post.status_code, 302)
        self.assertEqual(ProspectSource.objects.filter(search_result=self.result).count(), 1)

        post_again = self.client.post(reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]))
        self.assertEqual(post_again.status_code, 302)
        self.assertEqual(ProspectSource.objects.filter(search_result=self.result).count(), 1)

    def test_viewer_cannot_promote_or_enrich(self):
        self._login(self.viewer)
        promote = self.client.post(reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]))
        self.assertEqual(promote.status_code, 403)

        self._login(self.admin)
        self.client.post(reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]))
        source = ProspectSource.objects.get(search_result=self.result)

        self._login(self.viewer)
        enrich = self.client.post(
            reverse("operations_portal:prospecting_add_enrichment", args=[source.prospect_id]),
            {"field": ProspectEnrichment.Field.EMAIL, "value": "contato@example.com", "source_type": ProspectEnrichment.SourceType.MANUAL},
        )
        self.assertEqual(enrich.status_code, 403)

    def test_prospect_list_and_detail_show_provenance_and_enrichment(self):
        self._login(self.admin)
        self.client.post(reverse("operations_portal:prospecting_promote_search_result", args=[self.result.id]))
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
        promote = self.client.post(reverse("operations_portal:prospecting_promote_search_result", args=[self.other_result.id]))
        self.assertEqual(promote.status_code, 404)

        self._login(self.other_admin)
        self.client.post(reverse("operations_portal:prospecting_promote_search_result", args=[self.other_result.id]))
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
