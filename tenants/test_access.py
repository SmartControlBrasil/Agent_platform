from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.test import TestCase

from tenants.access import (
    ALL_CAPABILITIES,
    CAPABILITY_ASSISTANT_PROFILE_CHANGE,
    CAPABILITY_COMMERCIAL_MANAGE,
    CAPABILITY_COMMERCIAL_VIEW,
    CAPABILITY_HANDOFFS_CHANGE_STATUS,
    CAPABILITY_LEADS_RETRY_CRM,
    CAPABILITY_LEADS_VIEW,
    CAPABILITY_MEMBERSHIPS_MANAGE,
    CAPABILITY_PORTAL_VIEW_DASHBOARD,
    CAPABILITY_TENANT_MANAGE,
    CAPABILITY_TENANT_VIEW,
    get_accessible_tenants,
    get_membership_tenants,
    require_tenant_capability,
    user_has_tenant_capability,
)
from tenants.models import Tenant, TenantMembership


class TenantMembershipModelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="member", password="pass")
        self.tenant = Tenant.objects.create(name="Tenant", slug="tenant")

    def test_membership_is_unique_per_user_and_tenant(self):
        TenantMembership.objects.create(tenant=self.tenant, user=self.user, role=TenantMembership.Role.VIEWER)

        with self.assertRaises(IntegrityError):
            TenantMembership.objects.create(tenant=self.tenant, user=self.user, role=TenantMembership.Role.MANAGER)

    def test_roles_are_limited_to_initial_choices(self):
        values = {value for value, _label in TenantMembership.Role.choices}

        self.assertEqual(values, {"tenant_admin", "manager", "operator", "viewer"})

    def test_inactive_membership_does_not_grant_access(self):
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role=TenantMembership.Role.TENANT_ADMIN,
            is_active=False,
        )

        self.assertFalse(user_has_tenant_capability(self.user, self.tenant, CAPABILITY_PORTAL_VIEW_DASHBOARD))
        self.assertEqual(list(get_accessible_tenants(self.user)), [])
        self.assertEqual(list(get_membership_tenants(self.user)), [])


class TenantCapabilityMatrixTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")

    def user_with_role(self, role):
        user = get_user_model().objects.create_user(username=f"user-{role}", password="pass")
        TenantMembership.objects.create(tenant=self.tenant_a, user=user, role=role)
        return user

    def assert_caps(self, user, expected):
        for capability in ALL_CAPABILITIES:
            with self.subTest(capability=capability):
                self.assertEqual(user_has_tenant_capability(user, self.tenant_a, capability), capability in expected)

    def test_tenant_admin_has_all_capabilities_for_own_tenant(self):
        user = self.user_with_role(TenantMembership.Role.TENANT_ADMIN)

        self.assert_caps(user, ALL_CAPABILITIES)

    def test_manager_operator_and_viewer_follow_matrix_exactly(self):
        manager = self.user_with_role(TenantMembership.Role.MANAGER)
        self.assert_caps(
            manager,
            {
                CAPABILITY_PORTAL_VIEW_DASHBOARD,
                "conversations.view",
                CAPABILITY_LEADS_VIEW,
                CAPABILITY_LEADS_RETRY_CRM,
                "handoffs.view",
                CAPABILITY_HANDOFFS_CHANGE_STATUS,
                CAPABILITY_COMMERCIAL_VIEW,
                CAPABILITY_COMMERCIAL_MANAGE,
                CAPABILITY_TENANT_VIEW,
                CAPABILITY_TENANT_MANAGE,
                "assistant_profile.view",
                "knowledge_base.view",
                "knowledge_base.configure",
                "knowledge_base.operate",
            },
        )

        operator = self.user_with_role(TenantMembership.Role.OPERATOR)
        self.assert_caps(
            operator,
            {
                CAPABILITY_PORTAL_VIEW_DASHBOARD,
                "conversations.view",
                CAPABILITY_LEADS_VIEW,
                "handoffs.view",
                CAPABILITY_HANDOFFS_CHANGE_STATUS,
                CAPABILITY_COMMERCIAL_VIEW,
                CAPABILITY_COMMERCIAL_MANAGE,
                CAPABILITY_TENANT_VIEW,
                "knowledge_base.view",
                "knowledge_base.operate",
            },
        )

        viewer = self.user_with_role(TenantMembership.Role.VIEWER)
        self.assert_caps(
            viewer,
            {
                CAPABILITY_PORTAL_VIEW_DASHBOARD,
                "conversations.view",
                CAPABILITY_LEADS_VIEW,
                "handoffs.view",
                CAPABILITY_COMMERCIAL_VIEW,
                CAPABILITY_TENANT_VIEW,
                "knowledge_base.view",
            },
        )

    def test_superuser_has_all_capabilities_for_all_tenants(self):
        user = get_user_model().objects.create_superuser(username="admin", password="pass", email="admin@example.com")

        for capability in ALL_CAPABILITIES:
            self.assertTrue(user_has_tenant_capability(user, self.tenant_a, capability))
            self.assertTrue(user_has_tenant_capability(user, self.tenant_b, capability))

    def test_user_without_membership_or_wrong_tenant_has_no_capabilities(self):
        user = self.user_with_role(TenantMembership.Role.TENANT_ADMIN)
        stranger = get_user_model().objects.create_user(username="stranger", password="pass")

        self.assertFalse(user_has_tenant_capability(stranger, self.tenant_a, CAPABILITY_LEADS_VIEW))
        self.assertFalse(user_has_tenant_capability(user, self.tenant_b, CAPABILITY_MEMBERSHIPS_MANAGE))
        with self.assertRaises(PermissionDenied):
            require_tenant_capability(user, self.tenant_b, CAPABILITY_ASSISTANT_PROFILE_CHANGE)
