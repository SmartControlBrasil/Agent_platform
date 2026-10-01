"""Fase 16B.1 — intenção comercial early e contato mínimo (telefone OU e-mail)."""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from assistant_core.consultative_policy import COLLECTION_ACTIVE_KEY
from assistant_core.dialogue_memory import COMMERCIAL_INTENT_KEY
from assistant_core.consultative_policy import (
    COMMERCIAL_INTENT_DETECTED_AT_KEY,
    INTENT_TYPE_KEY,
)
from conversations.models import Conversation, HandoffRequest, Message
from integrations.models import OutboxEvent
from leads.models import LeadDraft
from leads.services.commercial import has_usable_contact, is_ready_for_commercial_notification
from tenants.models import AssistantProfile, Tenant


@override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
class CommercialIntentEarlyTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="smart-control-brasil",
            domain="scb.example",
            is_active=True,
        )
        AssistantProfile.objects.create(tenant=self.tenant_a, name="Lívia", is_active=True)
        self.tenant_b = Tenant.objects.create(
            name="Outro Tenant",
            slug="outro-tenant",
            domain="outro.example",
            is_active=True,
        )
        AssistantProfile.objects.create(tenant=self.tenant_b, name="Lívia B", is_active=True)

        original_post = self.client.post

        def post_with_request_id(path, *args, **kwargs):
            if path == "/api/chat/" and kwargs.get("content_type") == "application/json" and "data" in kwargs:
                try:
                    payload = json.loads(kwargs["data"])
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict) and "request_id" not in payload:
                    payload["request_id"] = str(uuid.uuid4())
                    kwargs["data"] = json.dumps(payload)
                    kwargs.setdefault("HTTP_X_LIVIA_REQUEST_ID", payload["request_id"])
            return original_post(path, *args, **kwargs)

        self.client.post = post_with_request_id

    def _chat(self, message: str, *, session_id: str, tenant=None):
        tenant = tenant or self.tenant_a
        payload = {
            "tenant": tenant.slug,
            "session_id": session_id,
            "message": message,
            "source_page": "https://www.smartcontrolbrasil.com.br/",
        }
        return self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _lead(self, session_id: str, tenant=None) -> LeadDraft | None:
        tenant = tenant or self.tenant_a
        conversation = Conversation.objects.filter(tenant=tenant, session_id=session_id).first()
        if conversation is None:
            return None
        return LeadDraft.objects.filter(tenant=tenant, conversation=conversation).first()

    def _qd(self, lead: LeadDraft | None) -> dict:
        if lead is None:
            return {}
        return dict(lead.qualification_data or {})

    def test_a_technical_question_without_commercial_intent(self):
        response = self._chat(
            "Estou com um erro intermitente no painel elétrico e a linha para de funcionar.",
            session_id="tech-a",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LeadDraft.objects.filter(tenant=self.tenant_a).count(), 0)
        self.assertEqual(HandoffRequest.objects.filter(tenant=self.tenant_a).count(), 0)

    def test_b_budget_creates_lead_with_early_intent(self):
        response = self._chat("Quero um orçamento.", session_id="budget-b")
        self.assertEqual(response.status_code, 200)
        lead = self._lead("budget-b")
        self.assertIsNotNone(lead)
        qd = self._qd(lead)
        self.assertTrue(qd.get(COLLECTION_ACTIVE_KEY))
        self.assertTrue(qd.get(COMMERCIAL_INTENT_KEY))
        self.assertTrue(qd.get("collection_trigger_reason"))
        self.assertTrue(qd.get(COMMERCIAL_INTENT_DETECTED_AT_KEY))
        self.assertIn(qd.get(INTENT_TYPE_KEY), {"budget", "commercial"})
        response2 = self._chat("Como funciona a manutenção preventiva?", session_id="budget-b")
        self.assertEqual(response2.status_code, 200)

    def test_c_name_and_phone_without_email(self):
        session = "contact-c"
        self._chat("Quero um orçamento para automação de painel industrial.", session_id=session)
        self._chat("Meu nome é João.", session_id=session)
        self._chat("Meu telefone é 11999999999.", session_id=session)
        lead = self._lead(session)
        self.assertIsNotNone(lead)
        self.assertEqual(lead.name, "João")
        self.assertTrue(has_usable_contact(lead))
        self.assertFalse(str(lead.email or "").strip())
        self.assertTrue(is_ready_for_commercial_notification(lead))

    def test_d_email_without_phone(self):
        session = "contact-d"
        self._chat("Quero uma cotação para sistema web.", session_id=session)
        self._chat("Me chamo Ana.", session_id=session)
        self._chat("Meu e-mail é ana@empresa.com.br", session_id=session)
        lead = self._lead(session)
        self.assertIsNotNone(lead)
        self.assertTrue(has_usable_contact(lead))
        self.assertFalse(str(lead.phone or "").strip())
        self.assertTrue(is_ready_for_commercial_notification(lead))

    def test_e_all_contact_fields_persisted(self):
        session = "contact-e"
        self._chat("Quero orçamento para automação industrial em galpão.", session_id=session)
        self._chat("Sou Carlos da Empresa XPTO.", session_id=session)
        self._chat("Telefone 11988887777 e e-mail carlos@xpto.com.br.", session_id=session)
        lead = self._lead(session)
        self.assertIsNotNone(lead)
        self.assertTrue(lead.name)
        self.assertTrue(lead.phone)
        self.assertTrue(lead.email)
        self.assertTrue(lead.company or "xpto" in lead.name.lower())

    def test_f_human_handoff_with_lead_and_idempotency(self):
        session = "handoff-f"
        first = self._chat("Quero falar com um atendente.", session_id=session)
        self.assertEqual(first.status_code, 200)
        lead = self._lead(session)
        self.assertIsNotNone(lead)
        self.assertTrue(self._qd(lead).get(COMMERCIAL_INTENT_KEY))
        handoffs = HandoffRequest.objects.filter(tenant=self.tenant_a, conversation=lead.conversation)
        self.assertEqual(handoffs.count(), 1)
        second = self._chat("Quero falar com um atendente.", session_id=session)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            HandoffRequest.objects.filter(tenant=self.tenant_a, conversation=lead.conversation).count(),
            1,
        )
        third = self._chat("Ainda tenho dúvidas técnicas sobre o serviço.", session_id=session)
        self.assertEqual(third.status_code, 200)

    def test_g_repeated_budget_same_lead_no_duplicate_outbox(self):
        session = "repeat-g"
        with patch("integrations.outbox.service.enqueue_lead_qualified") as enqueue:
            self._chat("Quero orçamento para CLP Mitsubishi.", session_id=session)
            self._chat("Quero orçamento.", session_id=session)
            self._chat("Preciso mesmo dessa cotação.", session_id=session)
            self.assertEqual(LeadDraft.objects.filter(tenant=self.tenant_a, conversation__session_id=session).count(), 1)
            if enqueue.call_count:
                keys = [call[0][0].pk for call in enqueue.call_args_list]
                self.assertEqual(len(set(keys)), 1)

    def test_h_passive_fields_with_budget_intent(self):
        session = "passive-h"
        self._chat(
            "Sou João da XPTO e meu telefone é 11999999999. Quero cotação de um painel.",
            session_id=session,
        )
        lead = self._lead(session)
        self.assertIsNotNone(lead)
        self.assertTrue(self._qd(lead).get(COMMERCIAL_INTENT_KEY))
        self.assertIn("joão", lead.name.lower())
        self.assertTrue(lead.phone)
        self.assertTrue(
            "xpto" in (lead.company or "").lower() or "xpto" in lead.name.lower()
        )

    def test_i_tenant_isolation(self):
        self._chat("Quero um orçamento.", session_id="shared-session", tenant=self.tenant_a)
        self._chat("Quero um orçamento.", session_id="shared-session", tenant=self.tenant_b)
        lead_a = self._lead("shared-session", tenant=self.tenant_a)
        lead_b = self._lead("shared-session", tenant=self.tenant_b)
        self.assertIsNotNone(lead_a)
        self.assertIsNotNone(lead_b)
        self.assertNotEqual(lead_a.pk, lead_b.pk)
        self.assertEqual(lead_a.tenant_id, self.tenant_a.pk)
        self.assertEqual(lead_b.tenant_id, self.tenant_b.pk)
        self.assertEqual(
            Conversation.objects.filter(session_id="shared-session").count(),
            2,
        )
        self.assertEqual(OutboxEvent.objects.filter(tenant=self.tenant_a).count(), 0)
