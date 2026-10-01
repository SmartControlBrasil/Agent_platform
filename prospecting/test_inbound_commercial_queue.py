"""Fase 16B.4 — leads inbound da Lívia na Minha Fila."""

from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from assistant_core.consultative_policy import mark_collection_active
from conversations.models import Conversation, HandoffRequest
from leads.models import LeadDraft
from operations_portal.prospecting_views import _apply_my_queue_filters, _my_queue_primary_cta_url
from prospecting.application.commercial_queue import (
    CommercialQueueCategory,
    INBOUND_KIND_ATTENDANT,
    INBOUND_KIND_QUOTE,
    INBOUND_KIND_QUALIFIED,
    SOURCE_INBOUND_LIVIA,
    SOURCE_OUTBOUND_PROSPECT,
    build_commercial_queue,
    commercial_queue_counters_from_items,
    compute_commercial_queue_action_now,
    compute_commercial_queue_counters,
)
from prospecting.application.qualification import qualify_prospect
from prospecting.models import Prospect, ProspectContact
from tenants.models import Tenant


class InboundLiviaCommercialQueueTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="operator-inbound-queue", password="pass")
        self.tenant = Tenant.objects.create(name="Smart Control Brasil", slug="smart-control-brasil-inbound-queue")
        self.other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant-inbound-queue")

    def _queue(self, *, tenant=None, **kwargs):
        return build_commercial_queue(tenant=tenant or self.tenant, compact=True, **kwargs)

    def _inbound(self, items=None, *, tenant=None):
        items = items if items is not None else self._queue(tenant=tenant)
        return [item for item in items if item.source == SOURCE_INBOUND_LIVIA]

    def _action_now(self, *, tenant=None):
        counters = compute_commercial_queue_counters(tenant=tenant or self.tenant)
        return compute_commercial_queue_action_now(counters), counters

    def _conversation(self, *, tenant=None, session_id=None, **kwargs):
        return Conversation.objects.create(
            tenant=tenant or self.tenant,
            session_id=session_id or str(uuid.uuid4()),
            **kwargs,
        )

    def _lead(self, *, tenant=None, conversation=None, commercial_intent=True, intent_type="budget", **kwargs):
        tenant = tenant or self.tenant
        conversation = conversation or self._conversation(tenant=tenant)
        lead = LeadDraft.objects.create(tenant=tenant, conversation=conversation, **kwargs)
        if commercial_intent:
            mark_collection_active(lead, reason="explicit_quote" if intent_type == "budget" else intent_type, intent_type=intent_type)
            lead.save(update_fields=["qualification_data", "updated_at"])
        return lead

    def _handoff(self, *, tenant=None, conversation=None, lead=None, **kwargs):
        tenant = tenant or self.tenant
        conversation = conversation or getattr(lead, "conversation", None) or self._conversation(tenant=tenant)
        defaults = {
            "tenant": tenant,
            "conversation": conversation,
            "lead_draft": lead,
            "reason": HandoffRequest.Reason.EXPLICIT_REQUEST,
            "status": HandoffRequest.Status.PENDING,
            "visitor_phone": kwargs.pop("visitor_phone", "11988887777"),
            "visitor_name": kwargs.pop("visitor_name", "João"),
        }
        defaults.update(kwargs)
        return HandoffRequest.objects.create(**defaults)

    def _outbound_ready(self, *, name="Hospital Outbound", identity_key=None, tenant=None):
        tenant = tenant or self.tenant
        prospect = Prospect.objects.create(
            tenant=tenant,
            display_name=name,
            identity_key=identity_key or f"outbound-{uuid.uuid4().hex[:12]}",
            priority=Prospect.Priority.MEDIUM,
        )
        qualify_prospect(
            tenant=tenant,
            prospect=prospect,
            qualification_status=Prospect.QualificationStatus.QUALIFIED,
            priority=Prospect.Priority.MEDIUM,
            qualification_note="",
            actor=self.user,
        )
        ProspectContact.objects.create(
            tenant=tenant,
            prospect=prospect,
            name="Contato",
            email=f"{prospect.identity_key}@example.com",
        )
        return prospect

    def test_a_budget_with_phone_enters_action_now(self):
        lead = self._lead(
            name="João Silva",
            company="XPTO",
            phone="11999999999",
            need_summary="Orçamento para três painéis.",
            intent_type="budget",
        )
        items = self._inbound()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.category, CommercialQueueCategory.INBOUND_HANDOFF)
        self.assertEqual(item.inbound_kind, INBOUND_KIND_QUOTE)
        self.assertEqual(item.lead_draft_id, lead.pk)
        self.assertEqual(item.phone, "11999999999")
        self.assertEqual(item.origin_label, "Lívia")
        action_now, counters = self._action_now()
        self.assertEqual(counters.inbound_handoff, 1)
        self.assertEqual(action_now, 1)

    def test_b_budget_with_email_without_phone_enters_queue(self):
        self._lead(name="Maria", email="maria@empresa.com.br", phone="", intent_type="quote")
        items = self._inbound()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].email, "maria@empresa.com.br")
        self.assertEqual(items[0].phone, "")
        action_now, _counters = self._action_now()
        self.assertEqual(action_now, 1)

    def test_c_intent_without_contact_is_not_action_now(self):
        self._lead(name="Sem Contato", need_summary="Quero automatizar o galpão.", intent_type="budget")
        items = self._inbound()
        self.assertEqual(items, [])
        action_now, counters = self._action_now()
        self.assertEqual(counters.inbound_handoff, 0)
        self.assertEqual(action_now, 0)

    def test_d_technical_question_does_not_enter(self):
        conversation = self._conversation()
        LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            need_summary="Erro intermitente no painel elétrico.",
            phone="11988887777",
        )
        self.assertEqual(self._inbound(), [])
        action_now, _counters = self._action_now()
        self.assertEqual(action_now, 0)

    def test_e_explicit_attendant_with_contact_is_one_high_priority_item(self):
        lead = self._lead(name="João", phone="11999999999", intent_type="budget")
        handoff = self._handoff(lead=lead, visitor_phone="11999999999", visitor_name="João")
        items = self._inbound()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].inbound_kind, INBOUND_KIND_ATTENDANT)
        self.assertEqual(items[0].priority, Prospect.Priority.HIGH)
        self.assertEqual(items[0].lead_draft_id, lead.pk)
        self.assertEqual(items[0].handoff_id, handoff.pk)
        action_now, _counters = self._action_now()
        self.assertEqual(action_now, 1)

    def test_f_handoff_without_lead_draft_enters_queue(self):
        conversation = self._conversation()
        handoff = self._handoff(
            conversation=conversation,
            lead=None,
            visitor_name="Carla",
            visitor_phone="11977776666",
            summary="Quero falar com um atendente.",
        )
        items = self._inbound()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].lead_draft_id, None)
        self.assertEqual(items[0].handoff_id, handoff.pk)
        self.assertEqual(items[0].inbound_kind, INBOUND_KIND_ATTENDANT)
        self.assertEqual(items[0].phone, "11977776666")

    def test_g_lead_and_handoff_same_cycle_are_deduplicated(self):
        lead = self._lead(name="João", phone="11999999999", intent_type="budget")
        self._handoff(lead=lead)
        self.assertEqual(len(self._inbound()), 1)

    def test_h_two_distinct_inbound_leads_are_two_items(self):
        self._lead(name="João", phone="11999999999", intent_type="budget")
        self._lead(name="Ana", email="ana@empresa.com.br", intent_type="quote")
        items = self._inbound()
        self.assertEqual(len(items), 2)
        names = {item.display_name for item in items}
        self.assertEqual(names, {"João", "Ana"})

    def test_i_first_human_action_removes_actionable_inbound(self):
        lead = self._lead(name="Atendido", phone="11999999999", intent_type="budget")
        lead.first_human_action_at = timezone.now()
        lead.commercial_status = LeadDraft.CommercialStatus.IN_PROGRESS
        lead.save(update_fields=["first_human_action_at", "commercial_status", "updated_at"])
        self.assertEqual(self._inbound(), [])
        action_now, _counters = self._action_now()
        self.assertEqual(action_now, 0)

    def test_j_resolved_or_cancelled_handoff_does_not_enter(self):
        conversation = self._conversation()
        self._handoff(conversation=conversation, lead=None, status=HandoffRequest.Status.RESOLVED)
        other = self._conversation()
        self._handoff(conversation=other, lead=None, status=HandoffRequest.Status.CANCELLED)
        self.assertEqual(self._inbound(), [])

    def test_k_tenant_isolation(self):
        self._lead(tenant=self.tenant, name="Lead A", phone="11999999999", intent_type="budget")
        items_b = self._inbound(tenant=self.other_tenant)
        self.assertEqual(items_b, [])
        action_now_b, _counters = self._action_now(tenant=self.other_tenant)
        self.assertEqual(action_now_b, 0)
        self.assertEqual(len(self._inbound(tenant=self.tenant)), 1)

    def test_l_outbound_prospect_still_appears(self):
        prospect = self._outbound_ready()
        items = self._queue()
        outbound = [item for item in items if item.prospect_id == prospect.id]
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].source, SOURCE_OUTBOUND_PROSPECT)
        self.assertEqual(outbound[0].category, CommercialQueueCategory.READY_FOR_OUTREACH)

    def test_m_combined_queue_has_outbound_and_inbound(self):
        prospect = self._outbound_ready(name="Hospital Combinado")
        lead = self._lead(name="Lead Combinado", phone="11999999999", intent_type="budget")
        items = self._queue()
        sources = {item.source for item in items}
        self.assertEqual(sources, {SOURCE_OUTBOUND_PROSPECT, SOURCE_INBOUND_LIVIA})
        self.assertTrue(any(item.prospect_id == prospect.id for item in items))
        self.assertTrue(any(item.lead_draft_id == lead.pk for item in items))

    def test_n_priority_attendant_then_quote_then_outbound(self):
        outbound = self._outbound_ready(name="Prospect Outbound")
        quote = self._lead(name="Orçamento", phone="11988887777", intent_type="budget")
        attendant_lead = self._lead(name="Atendente", phone="11977776666", intent_type="budget")
        self._handoff(lead=attendant_lead, visitor_name="Atendente", visitor_phone="11977776666")
        items = self._queue()
        inbound_then_outbound = [
            item
            for item in items
            if item.lead_draft_id in {quote.pk, attendant_lead.pk} or item.prospect_id == outbound.id
        ]
        self.assertEqual(
            [item.display_name for item in inbound_then_outbound],
            ["Atendente", "Orçamento", "Prospect Outbound"],
        )
        self.assertEqual(inbound_then_outbound[0].inbound_kind, INBOUND_KIND_ATTENDANT)
        self.assertEqual(inbound_then_outbound[1].inbound_kind, INBOUND_KIND_QUOTE)

    def test_o_action_now_counters_include_inbound(self):
        self._lead(name="João", phone="11999999999", intent_type="budget")
        self._outbound_ready(name="Pronto")
        action_now, counters = self._action_now()
        self.assertEqual(counters.inbound_handoff, 1)
        self.assertEqual(counters.ready_for_outreach, 1)
        self.assertEqual(action_now, 1)

    def test_p_inbound_filter_returns_only_inbound(self):
        self._lead(name="Inbound Filtro", phone="11999999999", intent_type="budget")
        self._outbound_ready(name="Outbound Filtro")
        items = _apply_my_queue_filters(self._queue(), category=CommercialQueueCategory.INBOUND_HANDOFF)
        self.assertTrue(items)
        self.assertTrue(all(item.source == SOURCE_INBOUND_LIVIA for item in items))

    def test_q_all_filter_returns_inbound_and_outbound(self):
        self._lead(name="Inbound Todos", phone="11999999999", intent_type="budget")
        self._outbound_ready(name="Outbound Todos")
        items = _apply_my_queue_filters(self._queue(), category="")
        sources = {item.source for item in items}
        self.assertEqual(sources, {SOURCE_INBOUND_LIVIA, SOURCE_OUTBOUND_PROSPECT})

    def test_r_cta_points_to_tenant_safe_commercial_routes(self):
        lead = self._lead(name="CTA Lead", phone="11999999999", intent_type="budget")
        handoff = self._handoff(
            conversation=self._conversation(),
            lead=None,
            visitor_name="CTA Handoff",
            visitor_phone="11888887777",
        )
        items = {item.display_name: item for item in self._inbound()}
        lead_url = _my_queue_primary_cta_url(item=items["CTA Lead"], waiting_send_id=None)
        handoff_url = _my_queue_primary_cta_url(item=items["CTA Handoff"], waiting_send_id=None)
        self.assertEqual(lead_url, reverse("operations_portal:commercial_lead_detail", args=[lead.pk]))
        self.assertEqual(handoff_url, reverse("operations_portal:commercial_handoff_detail", args=[handoff.pk]))
        self.assertTrue(lead_url.startswith("/painel/comercial/leads/"))
        self.assertTrue(handoff_url.startswith("/painel/comercial/handoffs/"))

    def test_s_query_count_stable_with_more_inbound(self):
        self._lead(name="Baseline Inbound", phone="11999999999", intent_type="budget")
        with CaptureQueriesContext(connection) as baseline:
            self._queue()
        baseline_count = len(baseline.captured_queries)
        for index in range(12):
            self._lead(name=f"Extra Inbound {index}", phone="11988887777", intent_type="budget")
        with CaptureQueriesContext(connection) as expanded:
            items = self._queue()
        self.assertEqual(len(self._inbound(items)), 13)
        self.assertEqual(len(expanded.captured_queries), baseline_count)

    def test_t_multiple_related_references_same_cycle_dedupe(self):
        lead = self._lead(name="Ciclo Único", phone="11999999999", intent_type="budget")
        self._handoff(lead=lead, visitor_name="Ciclo Único", summary="Primeiro pedido")
        self._handoff(lead=lead, visitor_name="Ciclo Único", summary="Segundo pedido")
        items = self._inbound()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].lead_draft_id, lead.pk)
        self.assertIsNotNone(items[0].handoff_id)

    def test_qualified_lead_waiting_contact_is_actionable(self):
        lead = self._lead(
            name="Qualificado",
            phone="11966665555",
            intent_type="commercial_interest",
            qualification_status=LeadDraft.QualificationStatus.QUALIFIED,
            commercial_status=LeadDraft.CommercialStatus.CONTACT_PENDING,
        )
        items = self._inbound()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].inbound_kind, INBOUND_KIND_QUALIFIED)
        self.assertEqual(items[0].lead_draft_id, lead.pk)

    def test_closed_or_won_lead_does_not_enter(self):
        won = self._lead(name="Ganho", phone="11999999999", intent_type="budget")
        won.commercial_status = LeadDraft.CommercialStatus.WON
        won.save(update_fields=["commercial_status", "updated_at"])
        closed = self._lead(name="Encerrado", phone="11888887777", intent_type="budget")
        closed.commercial_status = LeadDraft.CommercialStatus.CLOSED
        closed.save(update_fields=["commercial_status", "updated_at"])
        self.assertEqual(self._inbound(), [])
