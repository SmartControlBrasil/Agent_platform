from django.db import IntegrityError
from django.test import TestCase

from projects.models import Project
from tenants.models import Tenant


class ProjectModelTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.other_tenant = Tenant.objects.create(name="Tenant B", slug="tenant-b")

    def test_project_belongs_to_tenant(self):
        project = Project.objects.create(tenant=self.tenant, name="Site institucional", slug="site")

        self.assertEqual(project.tenant, self.tenant)
        self.assertTrue(project.is_active)

    def test_slug_is_unique_inside_tenant_only(self):
        Project.objects.create(tenant=self.tenant, name="Site", slug="site")
        Project.objects.create(tenant=self.other_tenant, name="Site", slug="site")

        with self.assertRaises(IntegrityError):
            Project.objects.create(tenant=self.tenant, name="Site 2", slug="site")

    def test_queries_can_be_tenant_scoped(self):
        project = Project.objects.create(tenant=self.tenant, name="Smart Sales", slug="smart-sales")
        Project.objects.create(tenant=self.other_tenant, name="Other", slug="other")

        self.assertEqual(list(Project.objects.filter(tenant=self.tenant)), [project])
