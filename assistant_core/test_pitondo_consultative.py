from __future__ import annotations

import json
import uuid

from django.core.cache import cache
from django.test import TestCase, override_settings

from conversations.models import Conversation, HandoffRequest
from knowledge_base.models import KnowledgeDocument
from leads.models import LeadDraft
from tenants.models import AssistantProfile, Tenant, TenantAllowedOrigin


@override_settings(
    ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"],
    LIVIA_AI_ENABLED=False,
    LIVIA_CHAT_RATE_LIMIT_ENABLED=False,
    LIVIA_LEAD_NOTIFICATIONS_ENABLED=False,
)
class PitondoConsultativeFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(
            name="Granimármores Pitondo",
            slug="granimarmores-pitondo",
            domain="https://www.granimarmorespitondo.com.br",
            is_active=True,
        )
        TenantAllowedOrigin.objects.create(tenant=self.tenant, origin="https://www.granimarmorespitondo.com.br")
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            initial_message="Olá! Sou a Lívia da Granimármores Pitondo. Como posso ajudar?",
            business_domain="marmoraria, pedras naturais e projetos sob medida",
            short_description="Qualifica projetos de bancadas e ambientes com pedras naturais.",
            notification_email="contato@granimarmorespitondo.com.br",
        )
        KnowledgeDocument.objects.create(
            tenant=self.tenant,
            title="Cozinhas e bancadas",
            slug="cozinhas-bancadas",
            content=(
                "A Granimármores Pitondo desenvolve bancadas de cozinha sob medida em granito e mármore. "
                "É possível prever recortes para cooktop e pia conforme o projeto."
            ),
            tags=["cozinha", "bancada", "cooktop", "granito"],
        )
        self.session_id = f"pitondo-{uuid.uuid4().hex[:8]}"

    def _chat(self, message: str, *, session_id: str | None = None) -> dict:
        rid = str(uuid.uuid4())
        response = self.client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "tenant": self.tenant.slug,
                    "session_id": session_id or self.session_id,
                    "request_id": rid,
                    "message": message,
                    "source_page": "https://www.granimarmorespitondo.com.br/",
                }
            ),
            content_type="application/json",
            HTTP_ORIGIN="https://www.granimarmorespitondo.com.br",
            HTTP_X_LIVIA_REQUEST_ID=rid,
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _assert_no_collection(self, reply: str):
        lowered = reply.lower()
        self.assertNotIn("seu nome", lowered)
        self.assertNotIn("nome da empresa", lowered)
        self.assertNotIn("telefone/whatsapp", lowered)

    def test_kitchen_countertop_flow_stays_consultative_until_budget(self):
        for message in (
            "Olá",
            "estou reformando minha cozinha",
            "quero fazer uma bancada",
            "vou colocar um cooktop",
            "qual material vocês recomendam?",
            "como funciona para medir?",
            "quanto custa?",
        ):
            payload = self._chat(message)
            if message != "Olá":
                self.assertNotEqual(payload.get("intent"), "greeting")
            self._assert_no_collection(payload["reply"])
            if message in {"quero fazer uma bancada", "vou colocar um cooktop", "qual material vocês recomendam?"}:
                from assistant_core.services.deterministic_synthesis import is_generic_fallback_reply

                self.assertFalse(is_generic_fallback_reply(payload["reply"]), payload["reply"])
                self.assertNotIn("robótica", payload["reply"].lower())
                self.assertNotIn("mitsubishi", payload["reply"].lower())

        budget = self._chat("quero um orçamento")
        lowered = budget["reply"].lower()
        self.assertTrue(
            any(token in lowered for token in ("chamar", "nome", "telefone", "e-mail", "email", "whatsapp")),
            budget["reply"],
        )

        self._chat("Maria Silva")
        self._chat("11999998888")
        lead = LeadDraft.objects.get(tenant=self.tenant)
        need = (lead.need_summary or "").lower()
        self.assertTrue("cozinha" in need or "bancada" in need)
        self.assertTrue(lead.name or lead.company)
        self.assertTrue(lead.phone or lead.email)

    def test_conceptual_quote_process_does_not_collect(self):
        self._chat("quero fazer um banheiro", session_id="bath")
        self._chat("preciso de bancada e nicho", session_id="bath")
        conceptual = self._chat("como faço para pedir orçamento?", session_id="bath")
        self._assert_no_collection(conceptual["reply"])
        explicit = self._chat("pode fazer um orçamento para mim", session_id="bath")
        self.assertTrue(
            any(token in explicit["reply"].lower() for token in ("chamar", "nome", "telefone", "whatsapp", "e-mail", "email"))
        )

    def test_human_handoff_from_gourmet_context(self):
        self._chat("vocês fazem áreas gourmet?", session_id="gourmet")
        self._chat("quero bancada para churrasqueira", session_id="gourmet")
        handoff = self._chat("quero falar com alguém", session_id="gourmet")
        lowered = handoff["reply"].lower()
        self.assertTrue(any(token in lowered for token in ("atendimento", "humano", "chamar", "nome", "telefone", "contato")))
        self.assertTrue(HandoffRequest.objects.filter(conversation__session_id="gourmet").exists())

    def test_notification_email_is_pitondo_not_smart_control(self):
        profile = AssistantProfile.objects.get(tenant=self.tenant)
        self.assertEqual(profile.notification_email, "contato@granimarmorespitondo.com.br")
        self.assertNotEqual(profile.notification_email, "comercial@smartcontrolbrasil.com.br")

    def test_cross_tenant_need_keywords_do_not_leak_into_scb_budget_path(self):
        """Pitondo keywords must validate stone needs without changing SCB ecommerce path."""
        from assistant_core.qualification.livia import is_valid_need_summary

        self.assertTrue(is_valid_need_summary("quero bancada de granito para cozinha"))
        self.assertTrue(is_valid_need_summary("preciso de uma loja virtual para ferragens"))
        self.assertFalse(is_valid_need_summary("quero orçamento"))
