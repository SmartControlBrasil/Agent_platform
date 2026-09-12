import json
import uuid

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings

from assistant_core.conversation_turns import is_generic_fallback_reply, lead_has_name_deferred
from assistant_core.prompts.livia import DEFAULT_REPLY
from assistant_core.summary import build_conversation_transcript, build_lead_notification_body
from conversations.models import Conversation, HandoffRequest, Message
from integrations.models import OutboxEvent
from integrations.outbox.handlers import LeadQualifiedHandler
from integrations.outbox.payloads import SCHEMA_VERSION
from leads.models import LeadDraft
from leads.services.lead_notification import LeadNotificationService
from tenants.models import Tenant


class MultiTurnCommercialConversationTests(TestCase):
    def setUp(self):
        cache.clear()
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
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="smart-control-brasil",
            domain="smart-control-brasil.example",
            is_active=True,
        )
        self.session_id = "multi-turn-ecommerce"

    def _chat(self, message: str, session_id: str | None = None) -> dict:
        response = self.client.post(
            "/api/chat/",
            data=json.dumps({
                "tenant": self.tenant.slug,
                "session_id": session_id or self.session_id,
                "message": message,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _assert_no_generic_fallback(self, reply: str):
        self.assertFalse(is_generic_fallback_reply(reply), reply)
        self.assertNotEqual(reply.strip(), DEFAULT_REPLY)

    def _assert_no_name_collection(self, reply: str):
        lowered = reply.lower()
        self.assertNotIn("seu nome", lowered)
        self.assertNotIn("nome da empresa", lowered)
        self.assertNotIn("nome ou o nome", lowered)

    def _assert_no_invented_commercials(self, reply: str):
        lowered = reply.lower()
        self.assertNotRegex(reply, r"\b\d+\s*(dias|semanas|meses)\b")
        self.assertNotIn("r$", lowered)
        self.assertNotIn("garantia de", lowered)
        self.assertNotIn("stripe", lowered)
        self.assertNotIn("shopify", lowered)

    def test_site_need_then_detail_then_budget_collects_only_on_budget(self):
        first = self._chat("preciso de um site", session_id="site-three-turn")
        self.assertEqual(first["intent"], "commercial_interest")
        self._assert_no_generic_fallback(first["reply"])
        self._assert_no_name_collection(first["reply"])
        self.assertFalse(HandoffRequest.objects.filter(conversation__session_id="site-three-turn").exists())

        second = self._chat("é um site institucional para minha empresa", session_id="site-three-turn")
        self._assert_no_generic_fallback(second["reply"])
        self._assert_no_name_collection(second["reply"])
        self.assertFalse(HandoffRequest.objects.filter(conversation__session_id="site-three-turn").exists())

        third = self._chat("quero um orçamento", session_id="site-three-turn")
        lowered = third["reply"].lower()
        self.assertTrue(
            any(token in lowered for token in ("chamar", "nome", "empresa", "telefone", "e-mail", "email")),
            third["reply"],
        )
        lead = LeadDraft.objects.get(conversation__session_id="site-three-turn")
        self.assertTrue((lead.qualification_data or {}).get("collection_active"))

    def test_consultative_flow_then_explicit_budget_collects(self):
        consultative = [
            "preciso de um site",
            "gostaria de uma loja virtual",
            "eu vendo produtos para serralheria",
            "vendo ferragens e ferramentas",
            "quanto tempo para elaborar uma loja completa?",
        ]
        timeline = None
        for message in consultative:
            payload = self._chat(message)
            self.assertEqual(payload["intent"], "commercial_interest")
            self._assert_no_generic_fallback(payload["reply"])
            self._assert_no_name_collection(payload["reply"])
            self._assert_no_invented_commercials(payload["reply"])
            timeline = payload

        self.assertTrue(any(token in timeline["reply"].lower() for token in ("prazo", "tempo", "estimativa")))

        budget = self._chat("quero um orçamento para essa loja")
        self.assertIn(budget["intent"], {"quote_request", "commercial_interest"})
        self.assertTrue(
            any(token in budget["reply"].lower() for token in ("chamar", "nome", "empresa", "telefone", "e-mail", "email")),
            budget["reply"],
        )

        name_turn = self._chat("Maria Silva")
        self.assertNotIn("qual é o seu nome", name_turn["reply"].lower())
        self._chat("11999998888")
        self._chat("maria@ferragens.example")
        self._chat("Ferragens Silva")
        lead = LeadDraft.objects.get()
        need = lead.need_summary.lower()
        self.assertTrue("loja" in need or "site" in need or "virtual" in need)
        self.assertTrue("ferragem" in need or "ferramenta" in need or "serralheria" in need)
        self.assertTrue(lead.name)
        self.assertTrue(lead.phone and lead.email)
        self.assertIn(lead.status, {LeadDraft.Status.QUALIFIED, LeadDraft.Status.SENT_TO_CRM})

    def test_smart_control_ecommerce_conversation_keeps_context(self):
        turns = [
            "preciso de um site",
            "gostaria de uma loja virtual",
            "não quero falar meu nome ainda",
            "eu vendo produtos para serralheria",
            "vendo ferragens e ferramentas",
            "quanto tempo para elaborar uma loja completa?",
        ]
        replies = []
        for message in turns:
            payload = self._chat(message)
            replies.append(payload["reply"])
            self.assertEqual(payload["intent"], "commercial_interest")
            self._assert_no_generic_fallback(payload["reply"])
            self._assert_no_invented_commercials(payload["reply"])
            self._assert_no_name_collection(payload["reply"])

        lead = LeadDraft.objects.get()
        need = lead.need_summary.lower()
        self.assertIn("site", need)
        self.assertTrue("loja" in need or "virtual" in need)
        self.assertTrue("serralheria" in need or "ferragens" in need or "ferramentas" in need)
        last = replies[-1].lower()
        self.assertTrue(any(token in last for token in ("prazo", "tempo", "estimativa")))

    def test_price_question_does_not_start_collection(self):
        price = self._chat("quanto custa um site?", session_id="price-q")
        self._assert_no_name_collection(price["reply"])
        self.assertTrue(any(token in price["reply"].lower() for token in ("investimento", "escopo", "varia", "orçamento")))
        budget = self._chat("quero um orçamento para meu site", session_id="price-q")
        lowered = budget["reply"].lower()
        self.assertTrue(
            any(token in lowered for token in ("chamar", "nome", "empresa", "telefone", "e-mail", "email", "necessidade", "precisa")),
            budget["reply"],
        )

    def test_human_handoff_request_starts_appropriate_flow(self):
        self._chat("preciso de um site", session_id="human")
        self._chat("gostaria de uma loja virtual", session_id="human")
        handoff = self._chat("quero falar com um especialista", session_id="human")
        self._assert_no_generic_fallback(handoff["reply"])
        lowered = handoff["reply"].lower()
        self.assertTrue(
            any(token in lowered for token in ("atendimento", "especialista", "nome", "telefone", "contato", "equipe")),
            handoff["reply"],
        )

    def test_name_deferral_and_question_variants(self):
        self._chat("preciso de um site institucional", session_id="variants")
        self._chat("gostaria de uma loja virtual", session_id="variants")
        refusal = self._chat("prefiro não informar meu nome agora", session_id="variants")
        self.assertEqual(refusal["intent"], "commercial_interest")
        self._assert_no_generic_fallback(refusal["reply"])

        products = self._chat("minha loja vende ferramentas", session_id="variants")
        self._assert_no_generic_fallback(products["reply"])
        self.assertIn("ferramentas", products["reply"].lower())

        for question, expected in (
            ("quanto tempo demora?", "prazo"),
            ("qual o prazo?", "prazo"),
            ("vocês fazem pagamento online?", "pagamento"),
            ("tem cálculo de frete?", "frete"),
        ):
            payload = self._chat(question, session_id="variants")
            self.assertEqual(payload["intent"], "commercial_interest")
            self._assert_no_generic_fallback(payload["reply"])
            self._assert_no_invented_commercials(payload["reply"])
            self.assertIn(expected, payload["reply"].lower())

        conversation = Conversation.objects.get(session_id="variants")
        self.assertFalse(conversation.is_qualified)

    def test_other_segments_stay_generic(self):
        clinic = self._chat("preciso de um site para minha clínica", session_id="clinic")
        self.assertEqual(clinic["intent"], "commercial_interest")
        self._assert_no_generic_fallback(clinic["reply"])
        self._assert_no_name_collection(clinic["reply"])

        industry = self._chat("quero um sistema web para a indústria", session_id="industry")
        self.assertEqual(industry["intent"], "commercial_interest")
        self._assert_no_name_collection(industry["reply"])

        clothes = self._chat("quero um ecommerce para loja de roupas", session_id="clothes")
        self.assertEqual(clothes["intent"], "commercial_interest")
        self._assert_no_name_collection(clothes["reply"])

        automation = self._chat("preciso de automação para a linha de produção", session_id="auto")
        self.assertEqual(automation["intent"], "commercial_interest")
        self._assert_no_generic_fallback(automation["reply"])
        self._assert_no_name_collection(automation["reply"])


class LeadReportAndNotificationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="smart-control-brasil",
            domain="smart-control-brasil.example",
            is_active=True,
        )
        self.conversation = Conversation.objects.create(
            tenant=self.tenant,
            session_id="report-session",
            source_page="https://www.smartcontrolbrasil.com.br/",
        )
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="preciso de um site")
        Message.objects.create(conversation=self.conversation, role=Message.Role.ASSISTANT, content="Qual objetivo do site?")
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="gostaria de uma loja virtual")
        Message.objects.create(conversation=self.conversation, role=Message.Role.ASSISTANT, content="Entendi ecommerce.")
        Message.objects.create(conversation=self.conversation, role=Message.Role.USER, content="vendo ferragens e ferramentas")
        self.lead = LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            name="Maria",
            company="Ferragens Silva",
            phone="11999998888",
            email="maria@example.com",
            need_summary="loja virtual para ferragens e ferramentas",
            status=LeadDraft.Status.QUALIFIED,
        )

    def test_report_contains_transcript_and_need(self):
        body = build_lead_notification_body(self.lead, timestamp="02/09/2026 12:00")
        transcript = build_conversation_transcript(self.conversation, lead_draft=self.lead)
        self.assertIn("Cliente:", transcript)
        self.assertIn("Lívia:", transcript)
        self.assertIn("ferragens", body.lower())
        self.assertIn("maria", body.lower())
        self.assertIn("smart-control-brasil", body.lower())
        self.assertIn("https://www.smartcontrolbrasil.com.br/", body)
        self.assertIn("Histórico da conversa:", body)

    @override_settings(LIVIA_LEAD_NOTIFICATIONS_ENABLED=True, LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=True)
    def test_lead_notification_is_idempotent(self):
        first = LeadNotificationService().notify(self.lead)
        second = LeadNotificationService().notify(self.lead)
        self.assertTrue(first.success)
        self.assertFalse(first.skipped)
        self.assertTrue(second.skipped)
        self.lead.refresh_from_db()
        self.assertIn("lead_notification_sent_at", self.lead.qualification_data)

    @override_settings(
        LIVIA_LEAD_NOTIFICATIONS_ENABLED=True,
        LIVIA_LEAD_NOTIFICATIONS_DRY_RUN=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=True,
    )
    def test_lead_qualified_handler_sends_email_once(self):
        from django.utils import timezone

        event = OutboxEvent.objects.create(
            tenant=self.tenant,
            event_type=OutboxEvent.EventType.LEAD_QUALIFIED,
            aggregate_type="LeadDraft",
            aggregate_id=str(self.lead.pk),
            event_id=uuid.uuid4(),
            deduplication_key=f"lead-notify-{self.lead.pk}",
            available_at=timezone.now(),
            payload={"schema_version": SCHEMA_VERSION, "lead_draft_id": self.lead.pk},
            status=OutboxEvent.Status.PENDING,
        )
        first = LeadQualifiedHandler().process(event)
        second = LeadQualifiedHandler().process(event)
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Novo lead da Lívia", mail.outbox[0].subject)
        self.assertIn("Cliente:", mail.outbox[0].body)
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.qualification_data.get("lead_notification_sent_at"))
        self.assertEqual(second.metadata["email"]["skipped"], True)
        self.assertEqual(len(mail.outbox), 1)
