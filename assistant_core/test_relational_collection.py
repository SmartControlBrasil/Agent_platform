"""Relational progressive collection — Fase 3.1."""

import uuid

from django.test import TestCase, override_settings

from assistant_core.consultative_policy import mark_collection_active
from assistant_core.continuity_policy import OFFER, STATUS_KEY
from assistant_core.services.livia_decision import LiviaDecisionService
from conversations.models import Conversation
from leads.models import LeadDraft
from leads.services.commercial import QualificationService
from tenants.models import Tenant

B2B_NEED = "Preciso automatizar o controle de temperatura de três câmaras na empresa."
PF_NEED = "Meu ar-condicionado de casa apresenta problema e desliga sozinho há três dias seguidos."
KB = "[KNOWLEDGE_BASE]\nConteúdo:\nO controle de temperatura das câmaras exige avaliar sensores e cargas. O Duno limpa galpões e opera de forma autônoma.\n[/KNOWLEDGE_BASE]"
PF_KB = "[KNOWLEDGE_BASE]\nConteúdo:\nAr-condicionado residencial que desliga sozinho pode indicar falha elétrica ou sensor. Avalie filtros e estabilidade elétrica.\n[/KNOWLEDGE_BASE]"
CONTACT_REASON = "Para nossa equipe retornar seu atendimento"


@override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
class RelationalCollectionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Equipe Técnica", slug="relational")
        self.conversation = Conversation.objects.create(tenant=self.tenant, session_id="relational")
        self.service = LiviaDecisionService()
        self.history = []

    def turn(self, text, knowledge=KB):
        result = self.service.generate_reply(
            self.history, text, conversation=self.conversation, knowledge_context=knowledge,
        )
        self.history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": result.reply}])
        return result

    def lead(self):
        return LeadDraft.objects.get(conversation=self.conversation)

    def promptable(self):
        return QualificationService().promptable_fields(self.lead())

    def establish_consultative_relationship(self):
        self.turn("O Duno consegue limpar um galpão?")
        self.turn("E consegue trabalhar várias horas?")

    def accept_continuity(self, need=B2B_NEED, knowledge=KB):
        lead = LeadDraft.objects.filter(conversation=self.conversation).first()
        if lead is None or (lead.qualification_data or {}).get(STATUS_KEY) != "pending":
            self.turn(need, knowledge=knowledge)
        return self.turn("Pode registrar", knowledge=knowledge)

    def ask_relational_name(self):
        self.establish_consultative_relationship()
        return self.turn("Como funciona o mapeamento?")

    def test_a_name_after_relationship(self):
        self.establish_consultative_relationship()
        reply = self.turn("Como funciona o mapeamento?")
        lead = self.lead()
        self.assertFalse(lead.qualification_data.get("collection_active"))
        self.assertIn("chamar", reply.reply.lower())
        self.assertTrue(lead.qualification_data.get("relational_name_asked"))

    def test_b_short_informational_does_not_ask_name(self):
        reply = self.turn("O que é automação industrial?")
        self.assertNotIn("chamar", reply.reply.lower())
        self.assertNotIn("nome", reply.reply.lower())

    def test_c_urgent_diagnostic_does_not_ask_name(self):
        self.establish_consultative_relationship()
        reply = self.turn("O equipamento está soltando fumaça.")
        self.assertNotIn("chamar", reply.reply.lower())

    def test_d_known_name_is_not_asked_again(self):
        self.establish_consultative_relationship()
        self.turn("Meu nome é Marcelo.")
        reply = self.turn("Como funciona o mapeamento?")
        self.assertEqual(self.lead().name, "Marcelo")
        self.assertNotIn("chamar", reply.reply.lower())

    def test_e_known_name_and_acceptance_goes_to_phone(self):
        self.ask_relational_name()
        self.turn("Marcelo")
        self.turn(B2B_NEED)
        reply = self.turn("Pode registrar")
        self.assertIn("telefone", reply.reply.lower())
        self.assertNotIn("chamar", reply.reply.lower())
        self.assertEqual(self.lead().name, "Marcelo")

    def test_f_known_phone_goes_to_email(self):
        self.ask_relational_name()
        self.turn("Marcelo")
        self.turn(B2B_NEED)
        self.turn("Pode registrar")
        reply = self.turn("11999999999")
        self.assertIn("e-mail", reply.reply.lower())
        self.assertNotIn("telefone", reply.reply.lower())

    def test_g_name_without_continuity_does_not_collect_contacts(self):
        self.ask_relational_name()
        self.turn("Marcelo")
        lead = self.lead()
        self.assertEqual(lead.name, "Marcelo")
        self.assertFalse(lead.qualification_data.get("collection_active"))
        self.assertFalse(lead.phone)
        self.assertFalse(lead.email)

    def test_h_b2b_context_asks_company(self):
        self.accept_continuity(B2B_NEED)
        self.turn("Marcelo Silva")
        self.turn("11999999999")
        reply = self.turn("marcelo@example.com")
        self.assertIn("continuidade", reply.reply.lower())
        self.assertEqual(self.promptable(), ["company"])

    def test_i_personal_flow_does_not_ask_company(self):
        reply = self.accept_continuity(PF_NEED, knowledge=PF_KB)
        self.assertIn("chamar", reply.reply.lower())
        self.turn("Ana", knowledge=PF_KB)
        self.turn("11999999999", knowledge=PF_KB)
        reply = self.turn("ana@example.com", knowledge=PF_KB)
        self.assertNotIn("empresa", reply.reply.lower())
        self.assertEqual(self.promptable(), [])
        self.assertEqual(self.lead().status, LeadDraft.Status.QUALIFIED)

    def test_j_known_company_is_not_asked_again(self):
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            name="Marcelo Silva",
            phone="11999999999",
            email="marcelo@example.com",
            company="Empresa XYZ",
            need_summary=B2B_NEED,
        )
        mark_collection_active(lead)
        reply = self.turn("Antes disso, como funciona o controle?")
        self.assertNotIn("empresa", reply.reply.lower())

    def test_k_privacy_notice_once(self):
        self.accept_continuity(B2B_NEED)
        self.turn("Marcelo Silva")
        self.turn("11999999999")
        self.turn("marcelo@example.com")
        replies = [item["content"] for item in self.history if item["role"] == "assistant"]
        self.assertEqual(sum(CONTACT_REASON in reply for reply in replies), 1)
        self.assertTrue(self.lead().qualification_data.get("contact_reason_shown"))

    def test_l_one_question_at_a_time(self):
        self.accept_continuity(B2B_NEED)
        reply = self.turn("Marcelo Silva")
        self.assertLessEqual(reply.reply.count("?"), 1)
        phone_prompts = [
            item["content"]
            for item in self.history
            if item["role"] == "assistant" and "telefone" in item["content"].lower()
        ]
        self.assertTrue(phone_prompts)
        self.assertIn(CONTACT_REASON, phone_prompts[0])
        self.assertLessEqual(phone_prompts[0].count("?"), 1)

    def test_m_continuity_offer_preserved(self):
        reply = self.turn(B2B_NEED)
        self.assertIn(OFFER, reply.reply)
        self.assertFalse(self.lead().qualification_data.get("collection_active"))

    def test_n_need_summary_preserved(self):
        self.accept_continuity(B2B_NEED)
        lead = self.lead()
        self.assertIn("temperatura", lead.need_summary.lower())
        self.turn("Marcelo Silva")
        self.assertIn("temperatura", self.lead().need_summary.lower())
        self.assertNotIn("necessidade principal", self.history[-1]["content"].lower())

    def test_spontaneous_phone_after_acceptance(self):
        self.ask_relational_name()
        self.turn("Marcelo")
        self.turn(B2B_NEED)
        self.turn("Pode registrar")
        self.turn("Meu WhatsApp é 11988887777")
        lead = self.lead()
        self.assertEqual(lead.phone, "11988887777")
        self.assertIn("e-mail", self.history[-1]["content"].lower())

    def test_spontaneous_email_after_phone(self):
        self.ask_relational_name()
        self.turn("Marcelo")
        self.turn(B2B_NEED)
        self.turn("Pode registrar")
        self.turn("11988887777")
        self.turn("Meu e-mail é marcelo@example.com")
        lead = self.lead()
        self.assertEqual(lead.email, "marcelo@example.com")

    def test_isolated_contact_question_does_not_ask_name(self):
        reply = self.turn("Qual o telefone de vocês?")
        self.assertNotIn("chamar", reply.reply.lower())

    def test_company_decline_only_when_asked(self):
        self.accept_continuity(B2B_NEED)
        self.turn("Marcelo Silva")
        self.turn("11999999999")
        self.turn("marcelo@example.com")
        self.turn("não tenho empresa")
        lead = self.lead()
        self.assertEqual(lead.status, LeadDraft.Status.QUALIFIED)
        self.assertEqual(lead.company, "")

    def test_relational_name_capture_keeps_collection_inactive(self):
        self.establish_consultative_relationship()
        self.turn("Como funciona o mapeamento?")
        self.turn("Carlos")
        lead = self.lead()
        self.assertEqual(lead.name, "Carlos")
        self.assertFalse(lead.qualification_data.get("collection_active"))

    def test_explicit_budget_still_activates_collection(self):
        reply = self.turn("Quero orçamento.")
        lead = self.lead()
        self.assertTrue(lead.qualification_data.get("collection_active"))
        self.assertNotIn(OFFER, reply.reply)

    def test_missing_fields_excludes_company_without_b2b(self):
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=Conversation.objects.create(tenant=self.tenant, session_id=str(uuid.uuid4())),
            need_summary=PF_NEED,
        )
        self.assertEqual(QualificationService().missing_fields(lead), ["name", "phone", "email"])

    def test_possible_enrichment_fields_with_b2b(self):
        lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=Conversation.objects.create(tenant=self.tenant, session_id=str(uuid.uuid4())),
            need_summary=B2B_NEED,
        )
        self.assertEqual(
            QualificationService().possible_enrichment_fields(lead, message=B2B_NEED),
            ["company"],
        )
