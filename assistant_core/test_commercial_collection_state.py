"""State machine comercial: coleta de slots, handoff humano e progressão determinística."""

from __future__ import annotations

from django.test import TestCase

from assistant_core.consultative_policy import (
    COLLECTION_ACTIVE_KEY,
    collection_already_active,
    detect_collection_trigger,
    is_explicit_human_handoff,
    CollectionTrigger,
)
from assistant_core.conversation_turns import classify_conversation_turn, is_need_enrichment
from assistant_core.discovery import analyze_message
from assistant_core.qualification import is_valid_name, is_valid_need_summary, message_fills_pending_slot
from assistant_core.services import LiviaDecisionService
from conversations.models import Conversation, HandoffRequest
from integrations.models import OutboxEvent
from leads.models import LeadDraft
from leads.services.commercial import QualificationService
from tenants.models import AssistantProfile, Tenant


class CommercialCollectionStateTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="scb-collection-state",
            domain="scb-collection.example",
            is_active=True,
        )
        self.other_tenant = Tenant.objects.create(
            name="Outro Tenant",
            slug="other-collection-state",
            domain="other-collection.example",
            is_active=True,
        )
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            business_domain="robótica e automação",
            short_description="Consultoria comercial e técnica.",
            human_handoff_enabled=True,
        )
        AssistantProfile.objects.create(
            tenant=self.other_tenant,
            name="Lívia",
            business_domain="software",
            short_description="Outro tenant.",
            human_handoff_enabled=True,
        )
        self.service = LiviaDecisionService()
        self.qualification = QualificationService()

    def _conversation(self, *, tenant=None, session_id: str = "collection-session") -> Conversation:
        return Conversation.objects.create(
            tenant=tenant or self.tenant,
            session_id=session_id,
        )

    def _history(self, *user_messages: str) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for msg in user_messages:
            items.append({"role": "user", "content": msg})
            items.append({"role": "assistant", "content": "ok"})
        return items

    def _lead(self, conversation: Conversation) -> LeadDraft:
        lead = LeadDraft.objects.filter(conversation=conversation).first()
        if lead is None:
            return conversation.lead_draft
        lead.refresh_from_db()
        return lead

    def _run_duno_smoke(self, conversation: Conversation | None = None):
        conversation = conversation or self._conversation()
        history: list[dict[str, str]] = []
        kb = (
            "[KNOWLEDGE_BASE]\nConteúdo:\n"
            "Duno (HygiBot) é um robô de limpeza para grandes áreas com modos lavar, varrer e aspirar.\n"
            "[/KNOWLEDGE_BASE]"
        )
        turns = [
            "quero saber mais sobre o Duno",
            "quero um orçamento",
            "Limpar meu galpão",
            "Gostaria que alguem entrasse em contato comigo",
        ]
        decisions = []
        for msg in turns:
            decision = self.service.generate_reply(
                history,
                msg,
                conversation=conversation,
                knowledge_context=kb if "Duno" in msg else "",
            )
            history.extend(
                [
                    {"role": "user", "content": msg},
                    {"role": "assistant", "content": decision.reply},
                ]
            )
            decisions.append(decision)
        return conversation, decisions

    def test_budget_starts_collection(self):
        conversation = self._conversation(session_id="budget-start")
        decision = self.service.generate_reply(
            self._history("quero saber mais sobre o Duno"),
            "quero um orçamento",
            conversation=conversation,
        )
        lead = self._lead(conversation)
        self.assertTrue((lead.qualification_data or {}).get(COLLECTION_ACTIVE_KEY))
        self.assertIn("qual é o seu nome", decision.reply.lower())

    def test_limpar_galpao_fills_need_summary(self):
        conversation = self._conversation(session_id="need-galpao")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        lead = self._lead(conversation)
        self.assertEqual(lead.need_summary, "Limpar meu galpão")
        self.assertTrue(is_valid_need_summary(lead.need_summary))

    def test_valid_need_summary_not_asked_again(self):
        conversation = self._conversation(session_id="need-no-repeat")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        decision = self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        self.assertNotIn("necessidade principal", decision.reply.lower())

    def test_next_slot_advances_to_name_or_company(self):
        conversation = self._conversation(session_id="next-slot-name")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        decision = self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        lowered = decision.reply.lower()
        self.assertTrue(any(token in lowered for token in ("nome", "empresa")))

    def test_um_galpao_not_name(self):
        self.assertFalse(is_valid_name("um galpão"))
        self.assertFalse(is_valid_name("galpão"))
        conversation = self._conversation(session_id="not-name-galpao")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        self.service.generate_reply(
            self._history("quero um orçamento", "Limpar meu galpão"),
            "um galpão",
            conversation=conversation,
        )
        lead = self._lead(conversation)
        self.assertFalse(is_valid_name(lead.name))
        self.assertFalse(is_valid_name(lead.company))

    def test_explicit_human_handoff_intents(self):
        phrases = (
            "Gostaria que alguem entrasse em contato comigo",
            "quero falar com alguém",
            "quero falar com um vendedor",
            "pode me ligar",
            "quero atendimento humano",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertTrue(is_explicit_human_handoff(phrase))
                self.assertEqual(detect_collection_trigger(phrase), CollectionTrigger.HUMAN)

    def test_human_handoff_priority_over_enrichment(self):
        phrase = "Gostaria que alguem entrasse em contato comigo"
        self.assertFalse(is_need_enrichment(phrase))

    def test_human_handoff_missing_contact_asks_only_contact(self):
        conversation, decisions = self._run_duno_smoke(self._conversation(session_id="handoff-contact-only"))
        last = decisions[-1]
        self.assertIsNotNone(last.handoff_request_id)
        lowered = last.reply.lower()
        self.assertIn("telefone", lowered)
        self.assertIn("e-mail", lowered)
        self.assertNotIn("ambiente", lowered)
        self.assertNotIn("piso", lowered)

    def test_human_handoff_with_contact_finalizes(self):
        conversation = self._conversation(session_id="handoff-with-contact")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        decision = self.service.generate_reply(
            self._history("quero um orçamento", "Limpar meu galpão"),
            "quero falar com um vendedor, meu telefone é 11999998888",
            conversation=conversation,
        )
        handoff = HandoffRequest.objects.get(conversation=conversation)
        self.assertEqual(handoff.visitor_phone, "11999998888")
        self.assertIn("registrei", decision.reply.lower())

    def test_handoff_does_not_duplicate(self):
        conversation = self._conversation(session_id="handoff-dedup")
        msg = "quero falar com um vendedor"
        self.service.generate_reply([], msg, conversation=conversation)
        self.service.generate_reply([], msg, conversation=conversation)
        self.assertEqual(HandoffRequest.objects.filter(conversation=conversation).count(), 1)

    def test_lead_does_not_duplicate(self):
        conversation = self._conversation(session_id="lead-dedup")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        self.service.generate_reply([], "Limpar meu galpão", conversation=conversation)
        self.assertEqual(LeadDraft.objects.filter(conversation=conversation).count(), 1)

    def test_outbox_does_not_duplicate_on_collection_turns(self):
        conversation = self._conversation(session_id="outbox-dedup")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        lead = self._lead(conversation)
        initial = OutboxEvent.objects.filter(
            event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
            aggregate_id=str(lead.pk),
        ).count()
        self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        final = OutboxEvent.objects.filter(
            event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
            aggregate_id=str(lead.pk),
        ).count()
        self.assertEqual(initial, final)

    def test_duno_subject_preserved_during_collection(self):
        conversation = self._conversation(session_id="duno-subject")
        kb = (
            "[KNOWLEDGE_BASE]\nConteúdo:\n"
            "Duno é um robô HygiBot para limpeza profissional.\n"
            "[/KNOWLEDGE_BASE]"
        )
        self.service.generate_reply([], "quero saber mais sobre o Duno", conversation=conversation, knowledge_context=kb)
        self.service.generate_reply(
            self._history("quero saber mais sobre o Duno"),
            "quero um orçamento",
            conversation=conversation,
        )
        self.service.generate_reply(
            self._history("quero saber mais sobre o Duno", "quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        lead = self._lead(conversation)
        data = lead.qualification_data or {}
        subject = str(data.get("active_knowledge_subject") or data.get("active_entity") or "")
        if subject:
            self.assertIn("duno", subject.lower())

    def test_technical_question_during_collection_still_works(self):
        conversation = self._conversation(session_id="tech-during-collection")
        kb = (
            "[KNOWLEDGE_BASE]\nConteúdo:\n"
            "Duno possui modo aspirar integrado ao sistema de limpeza.\n"
            "[/KNOWLEDGE_BASE]"
        )
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        decision = self.service.generate_reply(
            self._history("quero um orçamento", "Limpar meu galpão"),
            "mas antes, ele aspira?",
            conversation=conversation,
            knowledge_context=kb,
        )
        self.assertNotIn("necessidade principal", decision.reply.lower())

    def test_collection_resumes_correct_slot_after_technical_answer(self):
        conversation = self._conversation(session_id="resume-slot")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        self.service.generate_reply(
            self._history("quero um orçamento", "Limpar meu galpão"),
            "mas antes, ele aspira?",
            conversation=conversation,
            knowledge_context="[KNOWLEDGE_BASE]\nConteúdo:\nModo aspirar disponível.\n[/KNOWLEDGE_BASE]",
        )
        lead = self._lead(conversation)
        missing = self.qualification.missing_fields(lead)
        self.assertNotIn("need_summary", missing)
        self.assertIn("name", missing)

    def test_reply_does_not_concatenate_incompatible_templates(self):
        conversation, decisions = self._run_duno_smoke(self._conversation(session_id="no-hybrid"))
        handoff_reply = decisions[-1].reply.lower()
        self.assertNotIn("entendi, isso ajuda", handoff_reply)
        self.assertNotIn("atendemos projetos", handoff_reply)

    def test_tenant_isolation(self):
        conv_a = self._conversation(session_id="tenant-a")
        conv_b = self._conversation(tenant=self.other_tenant, session_id="tenant-b")
        self.service.generate_reply([], "quero um orçamento", conversation=conv_a)
        self.service.generate_reply([], "Limpar meu galpão", conversation=conv_b)
        lead_a = self._lead(conv_a)
        lead_b = self._lead(conv_b)
        self.assertEqual(lead_a.tenant_id, self.tenant.id)
        self.assertEqual(lead_b.tenant_id, self.other_tenant.id)
        self.assertEqual(lead_a.need_summary, "")
        self.assertEqual(lead_b.need_summary, "Limpar meu galpão")

    def test_old_session_does_not_contaminate_new(self):
        old = self._conversation(session_id="old-session")
        new = self._conversation(session_id="new-session")
        self.service.generate_reply([], "quero um orçamento", conversation=old)
        self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=old,
        )
        decision = self.service.generate_reply([], "quero um orçamento", conversation=new)
        new_lead = self._lead(new)
        self.assertEqual(new_lead.need_summary, "")
        self.assertIn("qual é o seu nome", decision.reply.lower())

    def test_field_sources_set_for_need_summary(self):
        conversation = self._conversation(session_id="field-sources")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        lead = self._lead(conversation)
        sources = lead.field_sources or {}
        self.assertTrue(
            sources.get("need_summary") or lead.need_summary,
            msg=f"field_sources={sources}, need_summary={lead.need_summary!r}",
        )

    def test_qualification_status_advances(self):
        conversation = self._conversation(session_id="qual-status")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        lead = self._lead(conversation)
        self.assertEqual(lead.qualification_status, LeadDraft.QualificationStatus.IN_PROGRESS)
        self.service.generate_reply(
            self._history("quero um orçamento"),
            "Limpar meu galpão",
            conversation=conversation,
        )
        lead.refresh_from_db()
        self.assertEqual(lead.qualification_status, LeadDraft.QualificationStatus.IN_PROGRESS)
        self.assertTrue(is_valid_need_summary(lead.need_summary))

    def test_message_fills_pending_need_slot(self):
        self.assertTrue(message_fills_pending_slot("Limpar meu galpão", "need_summary"))

    def test_collection_active_observable(self):
        conversation = self._conversation(session_id="collection-active-flag")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        lead = self._lead(conversation)
        self.assertTrue(collection_already_active(conversation, lead))

    def test_classify_routes_slot_answer_to_qualification(self):
        conversation = self._conversation(session_id="classify-slot")
        self.service.generate_reply([], "quero um orçamento", conversation=conversation)
        discovery = analyze_message("Limpar meu galpão")
        turn = classify_conversation_turn(
            current_message="Limpar meu galpão",
            history=self._history("quero um orçamento"),
            conversation=conversation,
            discovery=discovery,
        )
        self.assertEqual(turn.kind.value, "other")

    def test_full_duno_smoke_sequence(self):
        conversation, decisions = self._run_duno_smoke(self._conversation(session_id="full-smoke"))
        lead = self._lead(conversation)
        self.assertTrue(is_valid_need_summary(lead.need_summary))
        self.assertNotIn("necessidade principal", decisions[2].reply.lower())
        self.assertIsNotNone(decisions[3].handoff_request_id)
        self.assertIn("telefone", decisions[3].reply.lower())
        self.assertEqual(HandoffRequest.objects.filter(conversation=conversation).count(), 1)
