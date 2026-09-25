from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from audit.models import (
    ACTION_LEAD_ASSIGNED,
    ACTION_LEAD_COMMERCIAL_STATUS_CHANGED,
    ACTION_LEAD_NOTE_ADDED,
    AuditEvent,
)
from conversations.models import Conversation, HandoffRequest, Message
from leads.models import CommercialNote, LeadDraft
from leads.services.commercial_ops import mask_email, mask_phone, normalize_phone_for_whatsapp
from tenants.models import AssistantProfile, Tenant, TenantMembership


TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(SECURE_SSL_REDIRECT=False, STORAGES=TEST_STORAGES)
class CommercialPortalTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="scb-admin", password="pass")
        self.viewer = User.objects.create_user(username="scb-viewer", password="pass")
        self.pit_admin = User.objects.create_user(username="pit-admin", password="pass")
        self.superuser = User.objects.create_superuser(
            username="super", password="pass", email="super@example.com"
        )

        self.scb = Tenant.objects.create(name="Smart Control", slug="smart-control-brasil")
        self.pitondo = Tenant.objects.create(name="Pitondo", slug="granimarmores-pitondo")
        AssistantProfile.objects.create(
            tenant=self.scb,
            name="Lívia SCB",
            notification_email="comercial@smartcontrolbrasil.com.br",
        )
        AssistantProfile.objects.create(
            tenant=self.pitondo,
            name="Lívia Pitondo",
            notification_email="contato@granimarmorespitondo.com.br",
        )

        TenantMembership.objects.create(
            tenant=self.scb, user=self.admin, role=TenantMembership.Role.TENANT_ADMIN
        )
        TenantMembership.objects.create(
            tenant=self.scb, user=self.viewer, role=TenantMembership.Role.VIEWER
        )
        TenantMembership.objects.create(
            tenant=self.pitondo, user=self.pit_admin, role=TenantMembership.Role.TENANT_ADMIN
        )

        self.conv_scb = Conversation.objects.create(
            tenant=self.scb, session_id="scb-c1", source_page="https://smartcontrolbrasil.com.br/"
        )
        self.conv_pit = Conversation.objects.create(
            tenant=self.pitondo, session_id="pit-c1", source_page="https://pitondo.example/"
        )
        Message.objects.create(conversation=self.conv_scb, role=Message.Role.USER, content="Preciso de orçamento")
        Message.objects.create(conversation=self.conv_scb, role=Message.Role.ASSISTANT, content="Claro, me conta mais.")
        Message.objects.create(
            conversation=self.conv_scb, role=Message.Role.SYSTEM, content="internal policy"
        )

        self.lead_scb = LeadDraft.objects.create(
            tenant=self.scb,
            conversation=self.conv_scb,
            name="Cliente TESTE SCB",
            company="SCB Test",
            phone="11999990000",
            email="teste.scb@example.com",
            need_summary="Automação industrial TESTE",
            status=LeadDraft.Status.QUALIFIED,
            commercial_status=LeadDraft.CommercialStatus.NEW,
            dispatch_status=LeadDraft.DispatchStatus.DELIVERED,
            qualification_data={"lead_notification_sent_at": "2026-01-01T00:00:00Z"},
        )
        self.lead_pit = LeadDraft.objects.create(
            tenant=self.pitondo,
            conversation=self.conv_pit,
            name="Cliente TESTE Pitondo",
            phone="41988887777",
            email="teste.pit@example.com",
            need_summary="Bancada de granito TESTE",
            commercial_status=LeadDraft.CommercialStatus.NEW,
        )
        self.handoff_scb = HandoffRequest.objects.create(
            tenant=self.scb,
            conversation=self.conv_scb,
            lead_draft=self.lead_scb,
            status=HandoffRequest.Status.PENDING,
            priority=HandoffRequest.Priority.URGENT,
            reason=HandoffRequest.Reason.EMERGENCY_OR_URGENT,
            visitor_name="Cliente TESTE SCB",
            visitor_phone="11999990000",
            visitor_email="teste.scb@example.com",
            summary="Equipamento parado TESTE",
        )
        self.handoff_pit = HandoffRequest.objects.create(
            tenant=self.pitondo,
            conversation=self.conv_pit,
            lead_draft=self.lead_pit,
            status=HandoffRequest.Status.PENDING,
            priority=HandoffRequest.Priority.NORMAL,
            visitor_name="Cliente TESTE Pitondo",
            summary="Orçamento TESTE",
        )

        self.client = Client()

    def _login(self, user):
        self.client.force_login(user)

    def test_auth_required(self):
        url = reverse("operations_portal:commercial_dashboard")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)

    def test_viewer_can_list_but_not_manage(self):
        self._login(self.viewer)
        list_resp = self.client.get(reverse("operations_portal:commercial_lead_list"))
        self.assertEqual(list_resp.status_code, 200)
        self.assertContains(list_resp, "Cliente TESTE SCB")
        self.assertNotContains(list_resp, "Cliente TESTE Pitondo")

        detail = self.client.get(
            reverse("operations_portal:commercial_lead_detail", args=[self.lead_scb.pk])
        )
        self.assertEqual(detail.status_code, 200)
        # viewer: PII masked
        self.assertContains(detail, mask_phone(self.lead_scb.phone))
        self.assertContains(detail, mask_email(self.lead_scb.email))
        self.assertNotContains(detail, "11999990000")

        assign = self.client.post(
            reverse("operations_portal:commercial_lead_assign", args=[self.lead_scb.pk]),
            {"assigned_to": self.viewer.pk},
        )
        self.assertEqual(assign.status_code, 403)

    def test_tenant_isolation_scb_and_pitondo(self):
        self._login(self.admin)
        scb_list = self.client.get(reverse("operations_portal:commercial_lead_list"))
        self.assertContains(scb_list, "Cliente TESTE SCB")
        self.assertNotContains(scb_list, "Cliente TESTE Pitondo")

        forbidden = self.client.get(
            reverse("operations_portal:commercial_lead_detail", args=[self.lead_pit.pk])
        )
        self.assertEqual(forbidden.status_code, 404)

        self._login(self.pit_admin)
        pit_list = self.client.get(reverse("operations_portal:commercial_lead_list"))
        self.assertContains(pit_list, "Cliente TESTE Pitondo")
        self.assertNotContains(pit_list, "Cliente TESTE SCB")

        handoff_list = self.client.get(reverse("operations_portal:commercial_handoff_list"))
        self.assertContains(handoff_list, "Cliente TESTE Pitondo")
        self.assertNotContains(handoff_list, "Cliente TESTE SCB")

    def test_lead_detail_shows_notification_email_and_transcript(self):
        self._login(self.admin)
        detail = self.client.get(
            reverse("operations_portal:commercial_lead_detail", args=[self.lead_scb.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "comercial@smartcontrolbrasil.com.br")
        self.assertContains(detail, "Preciso de orçamento")
        self.assertContains(detail, "Claro, me conta mais.")
        self.assertNotContains(detail, "internal policy")
        self.assertContains(detail, "11999990000")
        self.assertContains(detail, "sent")

    def test_assign_status_note_and_audit(self):
        self._login(self.admin)
        assign = self.client.post(
            reverse("operations_portal:commercial_lead_assign", args=[self.lead_scb.pk]),
            {"assigned_to": self.admin.pk},
        )
        self.assertEqual(assign.status_code, 302)
        self.lead_scb.refresh_from_db()
        self.assertEqual(self.lead_scb.assigned_to_id, self.admin.pk)
        self.assertEqual(self.lead_scb.commercial_status, LeadDraft.CommercialStatus.IN_PROGRESS)
        self.assertIsNotNone(self.lead_scb.assigned_at)
        self.assertIsNotNone(self.lead_scb.first_human_action_at)
        self.assertTrue(
            AuditEvent.objects.filter(action=ACTION_LEAD_ASSIGNED, object_id=str(self.lead_scb.pk)).exists()
        )

        status = self.client.post(
            reverse("operations_portal:commercial_lead_status", args=[self.lead_scb.pk]),
            {
                "commercial_status": LeadDraft.CommercialStatus.QUALIFIED,
                "note": "Cliente pediu retorno amanhã TESTE",
            },
        )
        self.assertEqual(status.status_code, 302)
        self.lead_scb.refresh_from_db()
        self.assertEqual(self.lead_scb.commercial_status, LeadDraft.CommercialStatus.QUALIFIED)
        self.assertTrue(
            CommercialNote.objects.filter(lead_draft=self.lead_scb, body__icontains="retorno").exists()
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                action=ACTION_LEAD_COMMERCIAL_STATUS_CHANGED, object_id=str(self.lead_scb.pk)
            ).exists()
        )

        note = self.client.post(
            reverse("operations_portal:commercial_lead_note", args=[self.lead_scb.pk]),
            {"body": "Enviar catálogo TESTE"},
        )
        self.assertEqual(note.status_code, 302)
        self.assertTrue(
            AuditEvent.objects.filter(action=ACTION_LEAD_NOTE_ADDED, object_id=str(self.lead_scb.pk)).exists()
        )

        lost = self.client.post(
            reverse("operations_portal:commercial_lead_status", args=[self.lead_scb.pk]),
            {
                "commercial_status": LeadDraft.CommercialStatus.LOST,
                "lost_reason": "preco",
                "note": "Sem budget TESTE",
            },
        )
        self.assertEqual(lost.status_code, 302)
        self.lead_scb.refresh_from_db()
        self.assertEqual(self.lead_scb.commercial_status, LeadDraft.CommercialStatus.LOST)
        self.assertEqual(self.lead_scb.lost_reason, "preco")

    def test_handoff_detail_and_assign(self):
        self._login(self.admin)
        detail = self.client.get(
            reverse("operations_portal:commercial_handoff_detail", args=[self.handoff_scb.pk])
        )
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Equipamento parado TESTE")

        assign = self.client.post(
            reverse("operations_portal:commercial_handoff_assign", args=[self.handoff_scb.pk]),
            {"assigned_to": self.admin.pk},
        )
        self.assertEqual(assign.status_code, 302)
        self.handoff_scb.refresh_from_db()
        self.lead_scb.refresh_from_db()
        self.assertEqual(self.handoff_scb.assigned_to_id, self.admin.pk)
        self.assertEqual(self.lead_scb.assigned_to_id, self.admin.pk)

    def test_attendances_order_urgent_first(self):
        self._login(self.admin)
        resp = self.client.get(reverse("operations_portal:commercial_attendances"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Handoffs urgentes")
        self.assertContains(resp, "Cliente TESTE SCB")
        self.assertContains(resp, "Equipamento parado TESTE")
        # Urgent section appears before normal section heading content order.
        content = resp.content.decode()
        self.assertLess(content.find("Handoffs urgentes"), content.find("Handoffs normais"))
        self.assertLess(content.find("Handoffs normais"), content.find("Leads novos"))

    def test_dashboard_cards_and_query_budget(self):
        self._login(self.admin)
        url = reverse("operations_portal:commercial_dashboard")
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(len(ctx), 40)
        self.assertContains(resp, "Novos")

    def test_phone_helpers(self):
        self.assertEqual(normalize_phone_for_whatsapp("(11) 99999-0000"), "5511999990000")
        self.assertEqual(mask_phone("11999990000"), "***0000")
        self.assertEqual(mask_email("teste.scb@example.com"), "t***@example.com")

    def test_superuser_global_sees_both_tenants(self):
        self._login(self.superuser)
        session = self.client.session
        session["operations_portal_tenant_catalog"] = "all"
        session.save()
        resp = self.client.get(
            reverse("operations_portal:commercial_lead_list"),
            {"tenant": "global"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Cliente TESTE SCB")
        self.assertContains(resp, "Cliente TESTE Pitondo")
