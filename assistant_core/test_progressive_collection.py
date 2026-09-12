"""Progressive collection contracts, without external integrations."""

import uuid

from django.test import TestCase, override_settings

from assistant_core.consultative_policy import mark_collection_active
from assistant_core.continuity_policy import OFFER, STATUS_KEY
from assistant_core.services.livia_decision import LiviaDecisionService
from conversations.models import Conversation, HandoffRequest
from leads.models import LeadDraft
from leads.services.commercial import QualificationService
from tenants.models import Tenant

B2B_NEED = "Preciso automatizar o controle de temperatura de três câmaras na empresa."
PF_NEED = "Meu ar-condicionado de casa apresenta problema e desliga sozinho há três dias seguidos."
KB = "[KNOWLEDGE_BASE]\nConteúdo:\nO controle de temperatura das câmaras exige avaliar sensores e cargas. O Duno limpa galpões e opera de forma autônoma.\n[/KNOWLEDGE_BASE]"
PF_KB = "[KNOWLEDGE_BASE]\nConteúdo:\nAr-condicionado residencial que desliga sozinho pode indicar falha elétrica ou sensor. Avalie filtros e estabilidade elétrica.\n[/KNOWLEDGE_BASE]"
CONTACT_REASON = "Para nossa equipe retornar seu atendimento"


@override_settings(LIVIA_AI_ENABLED=False, LIVIA_RAG_ENABLED=False)
class ProgressiveCollectionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Equipe Técnica", slug="progressive")
        self.conversation = Conversation.objects.create(tenant=self.tenant, session_id="progressive")
        self.service = LiviaDecisionService()
        self.history = []

    def turn(self, text, knowledge=KB):
        result = self.service.generate_reply(
            self.history, text, conversation=self.conversation, knowledge_context=knowledge,
        )
        self.history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": result.reply}])
        return result.reply

    def lead(self):
        return LeadDraft.objects.get(conversation=self.conversation)

    def pending(self):
        return QualificationService().missing_fields(self.lead())

    def promptable(self):
        return QualificationService().promptable_fields(self.lead())

    def start(self, need=B2B_NEED, knowledge=KB):
        self.assertIn(OFFER, self.turn(need, knowledge=knowledge))
        reply = self.turn("Pode registrar")
        self.assertIn("chamar", reply.lower())
        self.assertNotIn("necessidade principal", reply)
        return reply

    def b2b_contacts(self):
        self.start()
        self.assertIn("telefone", self.turn("Marcelo Silva").lower())
        self.assertIn("e-mail", self.turn("11999999999").lower())
        self.assertIn("continuidade", self.turn("marcelo@example.com").lower())

    def pf_contacts(self):
        self.start(PF_NEED, knowledge=PF_KB)
        self.turn("Marcelo Silva")
        self.turn("11999999999")
        self.turn("marcelo@example.com")

    def assert_complete(self):
        lead = self.lead()
        self.assertEqual(lead.name, "Marcelo Silva")
        self.assertEqual(lead.phone, "11999999999")
        self.assertEqual(lead.email, "marcelo@example.com")
        self.assertIn("temperatura", lead.need_summary)
        self.assertEqual(lead.status, LeadDraft.Status.QUALIFIED)
        self.assertEqual(self.pending(), [])

    def test_full_company_flow(self):
        self.b2b_contacts()
        self.assertEqual(self.lead().status, LeadDraft.Status.QUALIFIED)
        self.assertEqual(self.promptable(), ["company"])
        self.turn("Empresa XYZ")
        self.assert_complete()
        self.assertIn("XYZ", self.lead().company)

    def test_personal_flow(self):
        self.pf_contacts()
        lead = self.lead()
        self.assertEqual(lead.status, LeadDraft.Status.QUALIFIED)
        self.assertEqual(lead.company, "")
        self.assertEqual(self.promptable(), [])

    def test_spontaneous_contacts_are_all_reused(self):
        self.start()
        reply = self.turn("Meu nome é Marcelo Silva, meu telefone é 11999999999 e meu e-mail é marcelo@example.com")
        self.assertEqual(self.pending(), [])
        self.assertIn("continuidade", reply)
        self.assertEqual(self.lead().name, "Marcelo Silva")

    def test_spontaneous_company_resolves_optional_slot(self):
        self.start()
        self.turn("Meu nome é Marcelo Silva, meu telefone é 11999999999 e meu e-mail é marcelo@example.com, sou da Empresa XYZ")
        self.assert_complete()
        self.assertIn("XYZ", self.lead().company)

    def test_known_need_is_not_asked_after_acceptance(self):
        self.start()
        self.assertNotIn("need_summary", self.pending())
        self.assertEqual(self.lead().qualification_data[STATUS_KEY], "accepted")

    def test_email_does_not_replace_phone(self):
        lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation,
            name="Marcelo Silva", email="marcelo@example.com", need_summary=B2B_NEED)
        self.assertIn("phone", QualificationService().missing_fields(lead))

    def test_phone_does_not_replace_email(self):
        lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation,
            name="Marcelo Silva", phone="11999999999", need_summary=B2B_NEED)
        self.assertIn("email", QualificationService().missing_fields(lead))

    def test_company_does_not_replace_name(self):
        lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation,
            company="Empresa XYZ", phone="11999999999", email="marcelo@example.com", need_summary=B2B_NEED)
        self.assertEqual(QualificationService().missing_fields(lead), ["name"])

    def test_need_is_last_when_unknown(self):
        self.turn("Quero orçamento")
        self.assertEqual(self.pending(), ["name", "phone", "email", "need_summary"])
        self.turn("Marcelo Silva")
        self.turn("11999999999")
        self.turn("marcelo@example.com")
        reply = self.turn("Preciso automatizar três câmaras na empresa.")
        self.assertEqual(self.pending(), [])
        self.assertNotIn("Em uma frase", reply)

    def test_privacy_appears_once(self):
        self.b2b_contacts()
        self.turn("Empresa XYZ")
        replies = [item["content"] for item in self.history if item["role"] == "assistant"]
        self.assertEqual(sum(CONTACT_REASON in reply for reply in replies), 1)
        self.assertTrue(self.lead().qualification_data["contact_reason_shown"])

    def test_one_question_at_a_time(self):
        self.b2b_contacts()
        self.turn("Empresa XYZ")
        for item in self.history:
            if item["role"] == "assistant":
                self.assertLessEqual(item["content"].count("?"), 1, item["content"])

    def test_invalid_email_does_not_advance(self):
        self.start()
        self.turn("Marcelo Silva")
        self.turn("11999999999")
        reply = self.turn("marcelo@")
        self.assertEqual(self.promptable()[0], "email")
        self.assertEqual(self.lead().email, "")
        self.assertIn("e-mail", reply)

    def test_invalid_phone_does_not_advance(self):
        self.start()
        self.turn("Marcelo Silva")
        self.turn("12345")
        self.assertEqual(self.promptable()[0], "phone")
        self.assertEqual(self.lead().phone, "")

    def test_interruption_at_every_slot(self):
        for slot, fields in (
            ("name", {}), ("phone", {"name": "Marcelo Silva"}),
            ("email", {"name": "Marcelo Silva", "phone": "11999999999"}),
            ("company", {"name": "Marcelo Silva", "phone": "11999999999", "email": "marcelo@example.com"}),
        ):
            with self.subTest(slot=slot):
                self.conversation = Conversation.objects.create(tenant=self.tenant, session_id=str(uuid.uuid4()))
                self.history = []
                lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation,
                    need_summary=B2B_NEED, qualification_data={STATUS_KEY: "accepted"}, **fields)
                mark_collection_active(lead)
                reply = self.turn("Antes disso, como funciona o controle de temperatura?")
                lead = self.lead()
                self.assertEqual(self.promptable()[0], slot)
                self.assertTrue(lead.qualification_data["collection_active"])
                self.assertEqual(lead.qualification_data[STATUS_KEY], "accepted")
                self.assertIn("temperatura", reply)
                for field, value in fields.items():
                    self.assertEqual(getattr(lead, field), value)

    def test_company_decline_variants(self):
        for text in ("não", "não tenho empresa", "sou pessoa física", "é particular", "não se aplica"):
            with self.subTest(text=text):
                self.conversation = Conversation.objects.create(tenant=self.tenant, session_id=str(uuid.uuid4()))
                self.history = []
                lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation,
                    name="Marcelo Silva", phone="11999999999", email="marcelo@example.com", need_summary=B2B_NEED)
                mark_collection_active(lead)
                self.turn(text)
                self.assertEqual(self.lead().status, LeadDraft.Status.QUALIFIED)
                self.assertEqual(self.lead().company, "")

    def test_explicit_human_request_does_not_require_email(self):
        self.turn("Quero falar com alguém, é urgente")
        self.assertTrue(HandoffRequest.objects.filter(conversation=self.conversation).exists())
        self.assertNotEqual(self.lead().status, LeadDraft.Status.QUALIFIED)

    def test_declining_offer_does_not_collect(self):
        self.turn(B2B_NEED)
        self.turn("Não precisa")
        self.assertFalse(self.lead().qualification_data.get("collection_active"))
        self.assertNotIn(CONTACT_REASON, self.history[-1]["content"])

    def test_legacy_record_remains_readable(self):
        lead = LeadDraft.objects.create(tenant=self.tenant, conversation=self.conversation,
            company="Empresa Antiga", email="antiga@example.com", status=LeadDraft.Status.QUALIFIED)
        from operations_portal.selectors import get_lead_detail
        self.assertEqual(get_lead_detail(lead.pk, tenant=self.tenant).pk, lead.pk)
        self.assertIn("name", QualificationService().missing_fields(lead))
        lead.refresh_from_db()
        self.assertEqual(lead.status, LeadDraft.Status.QUALIFIED)

    def _start_name_slot(self):
        self.start()
        lead = self.lead()
        self.assertEqual(self.promptable()[0], "name")
        return lead

    def test_need_enrichment_during_name_slot_a(self):
        lead = self._start_name_slot()
        before_need = lead.need_summary
        self.turn("Limpar meu galpão")
        lead = self.lead()
        self.assertEqual(lead.name, "")
        self.assertIn("name", self.promptable())
        self.assertIn("galpão", lead.need_summary.lower())
        self.assertNotEqual(lead.need_summary.strip(), before_need.strip())

    def test_need_enrichment_during_name_slot_b(self):
        lead = self._start_name_slot()
        before_need = lead.need_summary
        self.turn("Quero automatizar três câmaras frigoríficas.")
        lead = self.lead()
        self.assertEqual(lead.name, "")
        self.assertIn("name", self.promptable())
        self.assertIn("câmaras", lead.need_summary.lower())
        self.assertNotEqual(lead.need_summary.strip(), before_need.strip())

    def test_name_captured_during_name_slot_c(self):
        self._start_name_slot()
        self.turn("Meu nome é Marcelo.")
        lead = self.lead()
        self.assertEqual(lead.name, "Marcelo")
        self.assertNotIn("name", self.promptable())

    def test_knowledge_interruption_during_name_slot_d(self):
        lead = self._start_name_slot()
        before_need = lead.need_summary
        reply = self.turn("Antes disso, esse erro pode danificar a placa?")
        lead = self.lead()
        self.assertEqual(lead.name, "")
        self.assertEqual(self.promptable()[0], "name")
        self.assertTrue(lead.qualification_data.get("collection_active"))
        self.assertEqual(lead.need_summary.strip(), before_need.strip())
        self.assertGreater(len(reply.strip()), 20)
        self.assertNotIn("chamar", reply.lower())
