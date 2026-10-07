from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from operations_portal.access import SESSION_ACTIVE_TENANT_KEY
from tenants.models import Tenant, TenantMembership
from tools.application.identity import approve_pairing, consume_pairing, request_pairing
from tools.models import (
    ToolDefinition,
    ToolExecutor,
    ToolExecutorCapability,
    ToolExecutorCredential,
    ToolExecutorPairingRequest,
)

TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class ExecutorManagementPortalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user(username="tenant-manager", password="pass")
        self.operator = User.objects.create_user(username="tenant-operator", password="pass")
        self.viewer = User.objects.create_user(username="tenant-viewer", password="pass")
        self.other_admin = User.objects.create_user(username="other-admin", password="pass")
        self.multi_admin = User.objects.create_user(username="multi-admin", password="pass")

        self.tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a", domain="a.example")
        self.other_tenant = Tenant.objects.create(name="Tenant B", slug="tenant-b", domain="b.example")
        TenantMembership.objects.create(tenant=self.tenant, user=self.manager, role=TenantMembership.Role.MANAGER)
        TenantMembership.objects.create(tenant=self.tenant, user=self.operator, role=TenantMembership.Role.OPERATOR)
        TenantMembership.objects.create(tenant=self.tenant, user=self.viewer, role=TenantMembership.Role.VIEWER)
        TenantMembership.objects.create(tenant=self.other_tenant, user=self.other_admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.multi_admin, role=TenantMembership.Role.TENANT_ADMIN)
        TenantMembership.objects.create(tenant=self.other_tenant, user=self.multi_admin, role=TenantMembership.Role.TENANT_ADMIN)

        self.maps_tool = ToolDefinition.objects.get(slug="prospecting.search_google_maps")
        self.executor = ToolExecutor.objects.create(
            tenant=self.tenant,
            name="Executor A",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="exec-a",
            last_seen_at=timezone.now(),
        )
        self.other_executor = ToolExecutor.objects.create(
            tenant=self.other_tenant,
            name="Executor B",
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            public_id="exec-b",
        )
        ToolExecutorCapability.objects.create(executor=self.executor, tool_definition=self.maps_tool)
        self.client = Client()

    def _login(self, user, tenant=None):
        self.client.force_login(user)
        if tenant is not None:
            session = self.client.session
            session[SESSION_ACTIVE_TENANT_KEY] = tenant.pk
            session.save()

    def _new_pairing(self, name="Browser extension"):
        return request_pairing(
            executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
            requested_name=name,
        )

    def test_executor_management_is_tenant_scoped(self):
        self._login(self.manager)

        response = self.client.get(reverse("operations_portal:executor_management"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Executor A")
        self.assertContains(response, "Online")
        self.assertContains(response, "Google Maps")
        self.assertContains(response, "exec-a")
        self.assertNotContains(response, "Executor B")
        self.assertNotContains(response, "exec-b")

    def test_viewer_and_operator_can_view_but_cannot_pair(self):
        for user in (self.viewer, self.operator):
            self.client = Client()
            self._login(user)
            response = self.client.get(reverse("operations_portal:executor_management"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Executor A")
            self.assertContains(response, "Somente gestores")

            pairing = self._new_pairing(name=f"Denied {user.username}")
            denied = self.client.post(
                reverse("operations_portal:executor_pairing_claim"),
                {"pairing_code": pairing.pairing_code},
            )
            self.assertEqual(denied.status_code, 403)
            pairing.refresh_from_db()
            self.assertEqual(pairing.status, ToolExecutorPairingRequest.Status.PENDING)

    def test_valid_pairing_code_claims_for_active_tenant_and_enables_maps_capability(self):
        self._login(self.manager)
        pairing = self._new_pairing(name="Chrome Extension")
        formatted_code = f"{pairing.pairing_code[:4]}-{pairing.pairing_code[4:8]}-{pairing.pairing_code[8:]}".lower()

        response = self.client.post(
            reverse("operations_portal:executor_pairing_claim"),
            {"pairing_code": formatted_code},
        )
        pairing.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("operations_portal:executor_management"))
        self.assertEqual(pairing.status, ToolExecutorPairingRequest.Status.APPROVED)
        self.assertEqual(pairing.tenant_id, self.tenant.pk)
        self.assertIsNotNone(pairing.executor_id)
        self.assertEqual(pairing.executor.tenant_id, self.tenant.pk)
        self.assertTrue(
            ToolExecutorCapability.objects.filter(
                executor=pairing.executor,
                tool_definition=self.maps_tool,
                is_enabled=True,
            ).exists()
        )
        self.assertEqual(ToolExecutorCredential.objects.filter(executor=pairing.executor).count(), 0)

    def test_pairing_secret_is_not_exposed_by_hando_claim(self):
        self._login(self.manager)
        pairing = self._new_pairing(name="No Secret")

        response = self.client.post(
            reverse("operations_portal:executor_pairing_claim"),
            {"pairing_code": pairing.pairing_code},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "aep_")

    def test_non_existing_expired_consumed_and_foreign_codes_are_rejected(self):
        self._login(self.manager)

        missing = self.client.post(reverse("operations_portal:executor_pairing_claim"), {"pairing_code": "MISSING123"}, follow=True)
        self.assertContains(missing, "Código de pareamento não encontrado")

        expired = self._new_pairing(name="Expired")
        expired.expires_at = timezone.now() - timedelta(minutes=1)
        expired.save(update_fields=["expires_at"])
        expired_response = self.client.post(
            reverse("operations_portal:executor_pairing_claim"),
            {"pairing_code": expired.pairing_code},
            follow=True,
        )
        expired.refresh_from_db()
        self.assertContains(expired_response, "expirou")
        self.assertEqual(expired.status, ToolExecutorPairingRequest.Status.EXPIRED)

        consumed = approve_pairing(pairing=self._new_pairing(name="Consumed"), tenant=self.tenant, actor=self.manager)
        consume_pairing(pairing_id=consumed.pk, pairing_code=consumed.pairing_code)
        consumed_response = self.client.post(
            reverse("operations_portal:executor_pairing_claim"),
            {"pairing_code": consumed.pairing_code},
            follow=True,
        )
        self.assertContains(consumed_response, "já foi consumido")

        foreign = self._new_pairing(name="Foreign")
        foreign.tenant = self.other_tenant
        foreign.save(update_fields=["tenant"])
        foreign_response = self.client.post(
            reverse("operations_portal:executor_pairing_claim"),
            {"pairing_code": foreign.pairing_code},
            follow=True,
        )
        self.assertContains(foreign_response, "não pertence ao tenant ativo")
        self.assertFalse(ToolExecutor.objects.filter(name="Foreign", tenant=self.tenant).exists())

    def test_claim_rejects_posted_tenant_to_avoid_scope_switch(self):
        self._login(self.multi_admin, tenant=self.tenant)
        pairing = self._new_pairing(name="Malicious Tenant Switch")

        response = self.client.post(
            reverse("operations_portal:executor_pairing_claim"),
            {"pairing_code": pairing.pairing_code, "tenant": self.other_tenant.pk},
        )
        pairing.refresh_from_db()

        self.assertEqual(response.status_code, 403)
        self.assertEqual(pairing.status, ToolExecutorPairingRequest.Status.PENDING)
        self.assertIsNone(pairing.tenant_id)

    def test_csrf_is_enforced_for_hando_pairing_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        pairing = self._new_pairing(name="CSRF")

        response = client.post(
            reverse("operations_portal:executor_pairing_claim"),
            {"pairing_code": pairing.pairing_code},
        )

        self.assertEqual(response.status_code, 403)

    def test_control_plane_pairing_approval_is_preserved(self):
        self._login(self.multi_admin, tenant=self.tenant)
        pairing = self._new_pairing(name="Control Plane Still Works")

        list_response = self.client.get(reverse("control_plane:executor_pairing_list"))
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, pairing.pairing_code)

        approve_response = self.client.post(
            reverse("control_plane:executor_pairing_approve", args=[pairing.pk]),
            {"tenant_id": self.tenant.pk},
        )
        pairing.refresh_from_db()

        self.assertEqual(approve_response.status_code, 302)
        self.assertEqual(pairing.status, ToolExecutorPairingRequest.Status.APPROVED)
        self.assertEqual(pairing.tenant_id, self.tenant.pk)
