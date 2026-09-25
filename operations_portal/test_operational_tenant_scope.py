from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from conversations.models import Conversation
from tenants.models import Tenant, TenantMembership

TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class OperationalTenantSelectorTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_user(username="member", password="pass")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        self.tenant_c = Tenant.objects.create(name="Tenant C", slug="tenant-c")
        Conversation.objects.create(tenant=self.tenant_a, session_id="session-a")
        Conversation.objects.create(tenant=self.tenant_b, session_id="session-b")
        Conversation.objects.create(tenant=self.tenant_c, session_id="session-c")

    def test_regular_user_sees_only_active_membership_tenants(self):
        TenantMembership.objects.create(tenant=self.tenant_a, user=self.user, role=TenantMembership.Role.VIEWER)
        TenantMembership.objects.create(
            tenant=self.tenant_b,
            user=self.user,
            role=TenantMembership.Role.VIEWER,
            is_active=False,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("operations_portal:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tenant A")
        self.assertNotContains(response, 'value="%s"' % self.tenant_b.pk)
        self.assertNotContains(response, 'value="%s"' % self.tenant_c.pk)

    def test_superuser_operational_mode_lists_membership_tenants_only(self):
        admin = get_user_model().objects.create_superuser(username="admin", password="pass", email="a@example.com")
        TenantMembership.objects.create(tenant=self.tenant_a, user=admin, role=TenantMembership.Role.TENANT_ADMIN)
        self.client.force_login(admin)

        response = self.client.get(reverse("operations_portal:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["portal_show_all_tenants"])
        self.assertContains(response, "Tenant A")
        self.assertNotContains(response, 'value="%s"' % self.tenant_c.pk)
        self.assertContains(response, "Mostrar todos os tenants")

    def test_superuser_can_enable_and_disable_admin_catalog(self):
        admin = get_user_model().objects.create_superuser(username="admin2", password="pass", email="b@example.com")
        TenantMembership.objects.create(tenant=self.tenant_a, user=admin, role=TenantMembership.Role.TENANT_ADMIN)
        self.client.force_login(admin)

        response = self.client.get(reverse("operations_portal:dashboard"), {"portal_tenant_catalog": "all"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["portal_show_all_tenants"])
        self.assertContains(response, 'value="%s"' % self.tenant_c.pk)
        self.assertContains(response, "Modo operacional")

        response = self.client.get(reverse("operations_portal:dashboard"), {"portal_tenant_catalog": "operational"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["portal_show_all_tenants"])

    def test_superuser_direct_tenant_access_without_membership(self):
        admin = get_user_model().objects.create_superuser(username="admin3", password="pass", email="c@example.com")
        TenantMembership.objects.create(tenant=self.tenant_a, user=admin, role=TenantMembership.Role.VIEWER)
        self.client.force_login(admin)

        response = self.client.get(reverse("operations_portal:dashboard"), {"tenant": self.tenant_c.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tenant"], self.tenant_c)

    def test_regular_user_cannot_access_tenant_without_membership(self):
        TenantMembership.objects.create(tenant=self.tenant_a, user=self.user, role=TenantMembership.Role.VIEWER)
        self.client.force_login(self.user)

        response = self.client.get(reverse("operations_portal:dashboard"), {"tenant": self.tenant_c.pk})

        self.assertEqual(response.status_code, 403)

    def test_global_view_requires_admin_catalog_and_explicit_global(self):
        admin = get_user_model().objects.create_superuser(username="admin4", password="pass", email="d@example.com")
        TenantMembership.objects.create(tenant=self.tenant_a, user=admin, role=TenantMembership.Role.VIEWER)
        self.client.force_login(admin)

        response = self.client.get(reverse("operations_portal:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["portal_is_global"])

        session = self.client.session
        session["operations_portal_tenant_catalog"] = "all"
        session.save()
        response = self.client.get(reverse("operations_portal:dashboard"), {"tenant": "global"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["portal_is_global"])
        self.assertContains(response, "session-a")
        self.assertContains(response, "session-c")

    def test_mutations_still_require_specific_tenant(self):
        TenantMembership.objects.create(tenant=self.tenant_a, user=self.user, role=TenantMembership.Role.VIEWER)
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(reverse("operations_portal:prospecting_search_run_create")).status_code,
            403,
        )

    def test_scb_membership_appears_for_provisioned_superuser(self):
        scb = Tenant.objects.create(name="SCB", slug="smart-control-brasil")
        admin = get_user_model().objects.create_superuser(username="phase81_admin_like", password="pass", email="e@example.com")
        TenantMembership.objects.create(tenant=scb, user=admin, role=TenantMembership.Role.TENANT_ADMIN)
        self.client.force_login(admin)

        response = self.client.get(reverse("operations_portal:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tenant"], scb)
        self.assertContains(response, "SCB")
