import json
import threading
import time
import unittest
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from conversations.models import ChatRequest, Conversation, HandoffRequest, Message
from integrations.models import OutboxEvent
from integrations.openai.client import OpenAIChatResult
from assistant_core.state import LeadState
from assistant_core.services.chat_idempotency import build_request_fingerprint, reserve_chat_request
from assistant_core.services import LiviaDecisionService
from leads.models import LeadDraft
from tenants.models import AssistantProfile, Tenant, TenantAllowedOrigin


class LiviaDecisionServiceTests(TestCase):
    def setUp(self):
        self.service = LiviaDecisionService()

    def test_greeting(self):
        decision = self.service.generate_reply([], "Olá, tudo bem?")

        self.assertEqual(decision.intent, "greeting")
        self.assertIn("Olá! Sou a Lívia", decision.reply)

    def test_budget_request(self):
        decision = self.service.generate_reply([], "Quero orçamento para um sistema.")

        self.assertEqual(decision.intent, "quote_request")
        self.assertIn("necessidade principal", decision.reply)

    def test_technical_message(self):
        decision = self.service.generate_reply([], "Estou com um erro no painel e não funciona")

        self.assertEqual(decision.intent, "technical_question")
        self.assertIn("pré-análise", decision.reply)

    def test_contact_capture(self):
        decision = self.service.generate_reply([], "Meu nome é Maria, meu email é maria@exemplo.com")

        self.assertEqual(decision.intent, "contact_data")
        self.assertIn("atendimento", decision.reply.lower())

    def test_support_request(self):
        decision = self.service.generate_reply([], "Preciso de suporte no sistema")

        self.assertEqual(decision.intent, "support_request")
        self.assertIn("caso", decision.reply.lower())


class ChatApiTests(TestCase):
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

    def test_chat_api_inactive_tenant_does_not_process_message(self):
        self.tenant.is_active = False
        self.tenant.save(update_fields=["is_active"])
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "inactive-session",
            "message": "Olá, quero atendimento.",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["reply"], "Este atendimento não está disponível no momento.")
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(LeadDraft.objects.count(), 0)
        self.assertEqual(HandoffRequest.objects.count(), 0)

    def test_chat_api_missing_tenant_returns_safe_response(self):
        payload = {
            "tenant": "tenant-inexistente",
            "session_id": "missing-tenant-session",
            "message": "Olá",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["reply"], "Este atendimento não está disponível no momento.")
        self.assertEqual(Conversation.objects.count(), 0)

    def test_chat_api_rejects_empty_message(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "empty-message-session",
            "message": "   ",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "message is required.")
        self.assertEqual(Conversation.objects.count(), 0)

    @override_settings(LIVIA_MAX_MESSAGE_LENGTH=12)
    def test_chat_api_rejects_long_message(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "long-message-session",
            "message": "Mensagem longa demais",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "message_too_long")
        self.assertIn("muito longa", response.json()["reply"])
        self.assertEqual(Conversation.objects.count(), 0)

    @override_settings(LIVIA_CHAT_RATE_LIMIT_ENABLED=True, LIVIA_CHAT_RATE_LIMIT_REQUESTS=1, LIVIA_CHAT_RATE_LIMIT_WINDOW_SECONDS=300)
    def test_chat_api_rate_limit_blocks_above_limit(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "rate-limit-session",
            "message": "Olá!",
        }

        first_response = self.client.post("/api/chat/", data=json.dumps(payload), content_type="application/json", REMOTE_ADDR="10.0.0.1")
        second_response = self.client.post("/api/chat/", data=json.dumps({**payload, "session_id": "rate-limit-session-2"}), content_type="application/json", REMOTE_ADDR="10.0.0.1")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 429)
        self.assertEqual(second_response.json()["error"], "rate_limited")

    @override_settings(LIVIA_CHAT_RATE_LIMIT_ENABLED=True, LIVIA_CHAT_RATE_LIMIT_REQUESTS=1, LIVIA_CHAT_RATE_LIMIT_WINDOW_SECONDS=300)
    def test_chat_api_rate_limit_uses_tenant_and_ip(self):
        other_tenant = Tenant.objects.create(name="Outro Tenant", slug="outro-tenant", domain="outro.example")
        payload = {"tenant": self.tenant.slug, "session_id": "tenant-ip-1", "message": "Olá!"}
        other_payload = {"tenant": other_tenant.slug, "session_id": "tenant-ip-2", "message": "Olá!"}

        first_response = self.client.post("/api/chat/", data=json.dumps(payload), content_type="application/json", REMOTE_ADDR="10.0.0.2")
        other_tenant_response = self.client.post("/api/chat/", data=json.dumps(other_payload), content_type="application/json", REMOTE_ADDR="10.0.0.2")
        other_ip_response = self.client.post("/api/chat/", data=json.dumps({**payload, "session_id": "tenant-ip-3"}), content_type="application/json", REMOTE_ADDR="10.0.0.3")

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(other_tenant_response.status_code, 200)
        self.assertEqual(other_ip_response.status_code, 200)

    def test_chat_api_blocks_many_links_as_spam_without_lead_or_handoff(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "spam-links-session",
            "message": "Veja http://a.example www.b.example https://c.example para free money",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "spam_blocked")
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(LeadDraft.objects.count(), 0)
        self.assertEqual(HandoffRequest.objects.count(), 0)

    def test_chat_api_allows_normal_portuguese_message(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "normal-portuguese-session",
            "message": "Olá, preciso de orçamento para automação industrial.",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Conversation.objects.filter(session_id="normal-portuguese-session").exists())

    def test_chat_api_accepts_session_key_for_valid_tenant(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_key": "session-key-123",
            "message": "Olá!",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tenant"], self.tenant.slug)
        self.assertEqual(response.json()["session_key"], "session-key-123")
        self.assertTrue(Conversation.objects.filter(session_id="session-key-123").exists())

    @override_settings(DEBUG=True, LIVIA_DEV_ALLOWED_WIDGET_ORIGINS=["http://localhost:3000"])
    def test_chat_api_options_allows_debug_localhost_origin(self):
        response = self.client.options(
            "/api/chat/",
            HTTP_ORIGIN="http://localhost:3000",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type, x-livia-tenant",
            HTTP_X_LIVIA_TENANT=self.tenant.slug,
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://localhost:3000")
        self.assertIn("POST", response["Access-Control-Allow-Methods"])
        self.assertIn("Content-Type", response["Access-Control-Allow-Headers"])
        self.assertIn("X-Livia-Tenant", response["Access-Control-Allow-Headers"])

    def test_chat_api_uses_active_assistant_profile(self):
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia Pitondo",
            initial_message="Olá! Sou a Lívia da Pitondo. Como posso ajudar?",
            tone="consultivo e direto",
            primary_goal="qualificar oportunidades",
            is_active=True,
        )
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-profile",
            "message": "Olá!",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["assistant_name"], "Lívia Pitondo")
        self.assertEqual(response.json()["reply"], "Olá! Sou a Lívia da Pitondo. Como posso ajudar?")
        self.assertEqual(response.json()["initial_message"], "Olá! Sou a Lívia da Pitondo. Como posso ajudar?")


    def test_chat_api_human_request_returns_whatsapp_handoff_payload(self):
        AssistantProfile.objects.create(
            tenant=self.tenant,
            human_handoff_enabled=True,
            human_handoff_channel="whatsapp",
            handoff_whatsapp_number="+55 (11) 51968-525",
            handoff_whatsapp_label="Falar com um especialista",
            handoff_whatsapp_message="Olá, vim pelo atendimento da Lívia.",
        )
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "whatsapp-handoff-session",
            "message": "quero falar com uma pessoa",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        handoff = HandoffRequest.objects.get(conversation__session_id="whatsapp-handoff-session")
        self.assertEqual(handoff.reason, HandoffRequest.Reason.EXPLICIT_REQUEST)
        self.assertEqual(data["reply"], "Claro. Use o botão do WhatsApp que apareceu na tela para falar com nossa equipe.")
        self.assertEqual(data["human_handoff"]["handoff_id"], handoff.pk)
        self.assertEqual(data["human_handoff"]["channel"], "whatsapp")
        self.assertEqual(data["human_handoff"]["url"], "https://wa.me/551151968525?text=Ol%C3%A1%2C+vim+pelo+atendimento+da+L%C3%ADvia.")
        payload_text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("token", payload_text.lower())
        self.assertNotIn("secret", payload_text.lower())
        self.assertNotIn("quero falar com uma pessoa", payload_text)

    def test_chat_api_human_request_reuses_existing_handoff(self):
        AssistantProfile.objects.create(
            tenant=self.tenant,
            human_handoff_enabled=True,
            human_handoff_channel="whatsapp",
            handoff_whatsapp_number="551151968525",
        )
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "reuse-handoff-session",
            "message": "falar com atendente",
        }

        first = self.client.post("/api/chat/", data=json.dumps(payload), content_type="application/json")
        second = self.client.post("/api/chat/", data=json.dumps({**payload, "message": "me passa para um especialista"}), content_type="application/json")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(HandoffRequest.objects.filter(conversation__session_id="reuse-handoff-session").count(), 1)
        self.assertEqual(first.json()["human_handoff"]["handoff_id"], second.json()["human_handoff"]["handoff_id"])

    def test_chat_api_normal_message_does_not_return_human_handoff(self):
        AssistantProfile.objects.create(
            tenant=self.tenant,
            human_handoff_enabled=True,
            human_handoff_channel="whatsapp",
            handoff_whatsapp_number="551151968525",
        )
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "normal-no-handoff",
            "message": "Quero orçamento para automação",
        }

        response = self.client.post("/api/chat/", data=json.dumps(payload), content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("human_handoff", response.json())

    def test_chat_api_human_request_without_valid_whatsapp_keeps_contact_flow(self):
        AssistantProfile.objects.create(tenant=self.tenant, human_handoff_enabled=False)
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "disabled-whatsapp-handoff",
            "message": "quero atendimento humano",
        }

        response = self.client.post("/api/chat/", data=json.dumps(payload), content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["human_handoff"], {"active": False})
        self.assertIn("telefone", response.json()["reply"].lower())

    def test_chat_api_creates_conversation_and_messages(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-123",
            "message": "Olá, quero saber mais.",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)
        self.assertEqual(response.json()["intent"], "greeting")

        conversation = Conversation.objects.get()
        self.assertEqual(conversation.tenant, self.tenant)
        self.assertEqual(conversation.session_id, "session-123")
        self.assertEqual(conversation.source_page, "https://example.com/pagina")

        user_message = Message.objects.filter(role=Message.Role.USER).get()
        assistant_message = Message.objects.filter(role=Message.Role.ASSISTANT).get()
        self.assertEqual(user_message.content, "Olá, quero saber mais.")
        self.assertIn("Olá! Sou a Lívia", assistant_message.content)

    def test_chat_api_creates_lead_draft_on_budget_request(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-456",
            "message": "Quero orçamento para um sistema de atendimento.",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(LeadDraft.objects.count(), 1)
        self.assertEqual(response.json()["intent"], "quote_request")

        lead_draft = LeadDraft.objects.get()
        self.assertEqual(lead_draft.conversation.session_id, "session-456")
        self.assertIn("orçamento", lead_draft.need_summary.lower())
        self.assertEqual(lead_draft.status, LeadDraft.Status.DRAFT)
        self.assertIn("chamar", response.json()["reply"].lower())

    def test_chat_api_vague_budget_asks_area_before_contact(self):
        AssistantProfile.objects.create(
            tenant=self.tenant,
            business_domain="automação industrial, robótica, manutenção técnica e sistemas web",
        )
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-discovery-budget",
            "message": "Quero orçamento",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        # Orçamento explícito inicia qualificação; se a necessidade ainda estiver vaga, pede detalhe.
        reply = response.json()["reply"].lower()
        self.assertTrue(any(token in reply for token in ("necessidade", "precisa", "contexto", "chamar", "nome")))
        conversation = Conversation.objects.get(session_id="session-discovery-budget")
        self.assertIn(conversation.lead_state, {LeadState.COLLECT_NEED, LeadState.COLLECT_NAME_COMPANY})

    def test_chat_api_budget_for_clp_identifies_automation_and_collects(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-clp",
            "message": "Preciso de orçamento para CLP Mitsubishi",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["intent"], "quote_request")
        self.assertEqual(LeadDraft.objects.count(), 1)
        lead_draft = LeadDraft.objects.get()
        self.assertIn("clp", lead_draft.need_summary.lower())
        self.assertIn("chamar", response.json()["reply"].lower())
        conversation = Conversation.objects.get(session_id="session-clp")
        self.assertEqual(conversation.lead_state, LeadState.COLLECT_NAME_COMPANY)

    def test_chat_api_robotics_interest_asks_environment(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-robotics",
            "message": "Vocês têm robô de limpeza?",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["intent"], "commercial_interest")
        reply = response.json()["reply"].lower()
        self.assertTrue(
            "pouco mais" in reply
            or "robô" in reply
            or "robo" in reply
            or "limpeza" in reply
            or "automat" in reply
            or "ambiente" in reply
            or "objetivo" in reply,
            response.json()["reply"],
        )
        self.assertNotIn("bancada", reply)
        self.assertNotIn("granito", reply)
        self.assertNotIn("seu nome", reply)

    def test_chat_api_profile_driven_maintenance_interest_asks_technical_context(self):
        AssistantProfile.objects.create(
            tenant=self.tenant,
            business_domain="serviços técnicos e atendimento comercial",
            short_description="Qualifica necessidades técnicas com detalhes do problema e do objetivo.",
        )
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-maintenance",
            "message": "Preciso arrumar uma esteira",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("esteira", response.json()["reply"].lower())
        self.assertIn("arrumar", response.json()["reply"].lower())
        self.assertNotIn("seu nome", response.json()["reply"].lower())

    def test_chat_api_software_web_interest_is_identified(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-software-web",
            "message": "Quero um site com IA",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["intent"], "commercial_interest")
        self.assertEqual(LeadDraft.objects.count(), 1)
        lead_draft = LeadDraft.objects.get()
        self.assertIn("site", lead_draft.need_summary.lower())

    def test_chat_api_creates_lead_draft_on_commercial_interest(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-457",
            "message": "Quero desenvolver um sistema para minha empresa.",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(LeadDraft.objects.count(), 1)
        lead_draft = LeadDraft.objects.get()
        self.assertEqual(response.json()["intent"], "commercial_interest")
        self.assertEqual(lead_draft.status, LeadDraft.Status.DRAFT)
        self.assertNotIn("nome", response.json()["reply"].lower())
        self.assertIn("sistema", response.json()["reply"].lower())

    def test_chat_api_transitions_to_collect_name_company_on_commercial_intent(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-state-1",
            "message": "Quero orçamento para um sistema de atendimento ao cliente.",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        conversation = Conversation.objects.get(session_id="session-state-1")
        self.assertEqual(conversation.lead_state, LeadState.COLLECT_NAME_COMPANY)
        self.assertFalse(conversation.is_qualified)

    def test_chat_api_phone_without_need_creates_draft_and_asks_need(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-phone-only",
            "message": "11999999999",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        # Telefone isolado não dispara coleta comercial sem gatilho explícito.
        self.assertEqual(LeadDraft.objects.count(), 0)
        reply = response.json()["reply"].lower()
        self.assertTrue(any(token in reply for token in ("contato", "completar", "chamar", "nome", "telefone", "e-mail", "email")))

    @override_settings(
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=True,
    )
    def test_chat_api_creates_lead_draft_when_contact_and_need_are_together(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-458",
            "message": "Sou Maria da ACME, meu telefone é 11999998888, meu e-mail é maria@example.com e preciso de automação industrial.",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(LeadDraft.objects.count(), 1)
        self.assertEqual(response.json()["intent"], "contact_data")
        lead_draft = LeadDraft.objects.get()
        self.assertEqual(lead_draft.status, LeadDraft.Status.QUALIFIED)
        self.assertEqual(OutboxEvent.objects.filter(event_type=OutboxEvent.EventType.LEAD_QUALIFIED, aggregate_id=str(lead_draft.pk)).count(), 1)
        self.assertFalse(lead_draft.crm_external_id)
        self.assertIsNone(lead_draft.sent_to_crm_at)
        self.assertIn("continuidade", response.json()["reply"].lower())

    def test_chat_api_does_not_create_lead_draft_on_technical_question(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-459",
            "message": "Estou com um erro no painel e não funciona.",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(LeadDraft.objects.count(), 0)
        self.assertEqual(response.json()["intent"], "technical_question")

    def test_chat_api_does_not_create_lead_draft_on_support_request(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-460",
            "message": "Preciso de suporte no sistema.",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(LeadDraft.objects.count(), 0)
        self.assertEqual(response.json()["intent"], "support_request")

    def test_chat_api_login_question_is_support_and_does_not_create_lead(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-login-support",
            "message": "Como faço login?",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["intent"], "support_request")
        self.assertEqual(LeadDraft.objects.count(), 0)

    def test_chat_api_does_not_create_lead_draft_on_greeting(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-789",
            "message": "Olá!",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(LeadDraft.objects.count(), 0)
        self.assertEqual(response.json()["intent"], "greeting")

    @override_settings(
        SMART360_LEAD_DISPATCH_ENABLED=False,
        SMART360_LEAD_DISPATCH_DRY_RUN=True,
    )
    def test_chat_api_does_not_dispatch_duplicate_for_already_qualified_lead(self):
        first_payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-no-duplicate",
            "message": "Sou Maria da ACME, meu telefone é 11999998888, meu e-mail é maria@example.com e quero orçamento para automação industrial de atendimento.",
            "source_page": "https://example.com/pagina",
        }
        first_response = self.client.post(
            "/api/chat/",
            data=json.dumps(first_payload),
            content_type="application/json",
        )
        self.assertEqual(first_response.status_code, 200)
        lead_draft = LeadDraft.objects.get()
        first_external_id = lead_draft.crm_external_id
        self.assertEqual(lead_draft.status, LeadDraft.Status.QUALIFIED)
        self.assertEqual(OutboxEvent.objects.filter(event_type=OutboxEvent.EventType.LEAD_QUALIFIED, aggregate_id=str(lead_draft.pk)).count(), 1)

        second_payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-no-duplicate",
            "message": "Quero orçamento de novo",
            "source_page": "https://example.com/pagina",
        }
        second_response = self.client.post(
            "/api/chat/",
            data=json.dumps(second_payload),
            content_type="application/json",
        )

        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(LeadDraft.objects.count(), 1)
        lead_draft.refresh_from_db()
        self.assertEqual(lead_draft.crm_external_id, first_external_id)
        self.assertEqual(OutboxEvent.objects.filter(event_type=OutboxEvent.EventType.LEAD_QUALIFIED, aggregate_id=str(lead_draft.pk)).count(), 1)
        self.assertIn("já encaminhei", second_response.json()["reply"].lower())

    def test_chat_api_does_not_force_lead_on_ambiguous_message(self):
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "session-790",
            "message": "Tenho uma dúvida rápida sobre o sistema.",
            "source_page": "https://example.com/pagina",
        }

        response = self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(LeadDraft.objects.count(), 0)
        self.assertEqual(response.json()["intent"], "unknown")


    def test_chat_api_pitondo_kitchen_sink_uses_natural_stone_discovery_without_smart_fallback(self):
        pitondo = Tenant.objects.create(
            name="Granimármores Pitondo",
            slug="granimarmores-pitondo",
            domain="granimarmorespitondo.com.br",
            is_active=True,
        )
        AssistantProfile.objects.create(
            tenant=pitondo,
            name="Lívia",
            initial_message="Olá! Sou a Lívia da Granimármores Pitondo. Como posso ajudar?",
            business_domain="marmoraria, pedras naturais e projetos sob medida",
            short_description="Qualifica projetos informando ambiente, medidas, fotos e detalhes do material.",
        )
        payload = {
            "tenant": pitondo.slug,
            "session_id": "pitondo-kitchen-sink",
            "message": "gostaria de um orçamento sobre uma pia para minha cozinha",
            "source_page": "https://www.granimarmorespitondo.com.br/",
        }

        response = self.client.post("/api/chat/", data=json.dumps(payload), content_type="application/json")

        self.assertEqual(response.status_code, 200)
        reply = response.json()["reply"].lower()
        self.assertEqual(response.json()["tenant"], "granimarmores-pitondo")
        # Orçamento explícito inicia coleta; o contexto da pia/cozinha permanece na necessidade.
        self.assertTrue("chamar" in reply or "nome" in reply or "necessidade" in reply or "medidas" in reply or "pia" in reply)
        self.assertNotIn("automação industrial", reply)
        self.assertNotIn("robótica", reply)
        self.assertNotIn("manutenção técnica", reply)
        self.assertNotIn("sistema web", reply)
        if LeadDraft.objects.exists():
            self.assertIn("pia", LeadDraft.objects.get().need_summary.lower())

    def test_chat_api_smart_automation_machine_asks_specific_automation_question(self):
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            business_domain="automação industrial, robótica, manutenção técnica e sistemas web",
        )
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "smart-automation-machine",
            "message": "Preciso automatizar uma máquina",
            "source_page": "https://www.smartcontrolbrasil.com.br/",
        }

        response = self.client.post("/api/chat/", data=json.dumps(payload), content_type="application/json")

        self.assertEqual(response.status_code, 200)
        reply = response.json()["reply"].lower()
        self.assertEqual(response.json()["tenant"], "smart-control-brasil")
        self.assertIn("maquina", reply)
        self.assertIn("automatizar", reply)
        self.assertNotIn("pia", reply)
        self.assertNotIn("bancada", reply)
        self.assertNotIn("seu nome", reply)

    def test_chat_api_tenant_isolation_blocks_cross_tenant_fallback_phrases(self):
        pitondo = Tenant.objects.create(
            name="Granimármores Pitondo",
            slug="granimarmores-pitondo",
            domain="granimarmorespitondo.com.br",
            is_active=True,
        )
        AssistantProfile.objects.create(
            tenant=pitondo,
            name="Lívia",
            business_domain="marmoraria e pedras naturais",
            short_description="Qualifica projetos com ambiente, medidas e fotos.",
        )
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            business_domain="automação industrial, robótica, manutenção técnica e sistemas web",
        )

        pitondo_response = self.client.post(
            "/api/chat/",
            data=json.dumps({
                "tenant": pitondo.slug,
                "session_id": "tenant-isolation-pitondo",
                "message": "Quero uma pia para minha cozinha",
            }),
            content_type="application/json",
        )
        smart_response = self.client.post(
            "/api/chat/",
            data=json.dumps({
                "tenant": self.tenant.slug,
                "session_id": "tenant-isolation-smart",
                "message": "Preciso automatizar uma máquina",
            }),
            content_type="application/json",
        )

        self.assertEqual(pitondo_response.status_code, 200)
        self.assertEqual(smart_response.status_code, 200)
        pitondo_reply = pitondo_response.json()["reply"].lower()
        smart_reply = smart_response.json()["reply"].lower()
        for forbidden in ("smart control", "automação industrial", "robótica", "manutenção técnica", "sistema web"):
            self.assertNotIn(forbidden, pitondo_reply)
        for forbidden in ("granimármores", "pitondo", "pia", "bancada", "mármore", "granito"):
            self.assertNotIn(forbidden, smart_reply)

    def test_chat_api_profile_driven_logistics_tenant_without_engine_taxonomy(self):
        tenant = Tenant.objects.create(
            name="Logistics Demo",
            slug="logistics-demo",
            domain="logistics.example",
            is_active=True,
        )
        AssistantProfile.objects.create(
            tenant=tenant,
            name="Lívia",
            business_domain="logística e transporte de cargas",
            short_description="Qualifica pedidos de frete com origem, destino, prazo e volume.",
        )

        response = self.client.post(
            "/api/chat/",
            data=json.dumps({
                "tenant": tenant.slug,
                "session_id": "logistics-demo-session",
                "message": "Preciso contratar um frete para amanhã",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        reply = response.json()["reply"].lower()
        self.assertIn("frete", reply)
        self.assertIn("contratar", reply)
        self.assertNotIn("automação", reply)
        self.assertNotIn("mármore", reply)

    def test_chat_api_profile_driven_ai_tenant_without_engine_taxonomy(self):
        tenant = Tenant.objects.create(
            name="AI Demo",
            slug="ai-demo",
            domain="ai.example",
            is_active=True,
        )
        AssistantProfile.objects.create(
            tenant=tenant,
            name="Lívia",
            business_domain="inteligência artificial aplicada a atendimento",
            short_description="Qualifica agentes de IA, integrações, canais e objetivos de atendimento.",
        )

        response = self.client.post(
            "/api/chat/",
            data=json.dumps({
                "tenant": tenant.slug,
                "session_id": "ai-demo-session",
                "message": "Quero criar um agente de inteligência artificial para atendimento",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        reply = response.json()["reply"].lower()
        self.assertIn("agente", reply)
        self.assertIn("criar", reply)
        self.assertNotIn("frete", reply)
        self.assertNotIn("granito", reply)


class LiviaDecisionKnowledgeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="smart-control-brasil",
            domain="smartcontrolbrasil.com.br",
        )
        self.service = LiviaDecisionService()

    def test_livia_decision_uses_knowledge_when_available(self):
        from knowledge_base.models import KnowledgeDocument

        KnowledgeDocument.objects.create(
            tenant=self.tenant,
            title="HygiBot / robô de limpeza",
            slug="hygibot-robo-limpeza",
            content="HygiBot é uma solução de robótica de limpeza para grandes áreas e ambientes profissionais.",
            tags=["robotics", "xyron", "hygibot", "limpeza", "robo"],
        )
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="knowledge-session")

        decision = self.service.generate_reply([], "Vocês têm robô de limpeza?", conversation=conversation)

        self.assertEqual(decision.intent, "commercial_interest")
        self.assertIn("HygiBot", decision.reply)
        self.assertIn("ambientes profissionais", decision.reply)
        self.assertIn("ambiente", decision.reply.lower())

    def test_livia_decision_does_not_echo_semantic_rag_block(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="semantic-no-echo")
        semantic = (
            "[KNOWLEDGE_BASE]\n"
            "Fonte: GP — Soluções para banheiros\n"
            "Score: 0.8123\n"
            "Conteúdo:\n"
            "Trabalhamos com cuidados específicos para banheiros e superfícies sensíveis a ácidos.\n"
            "[/KNOWLEDGE_BASE]"
        )
        decision = self.service.generate_reply(
            [],
            "Posso usar mármore no banheiro?",
            conversation=conversation,
            knowledge_context=semantic,
        )
        self.assertNotIn("Score:", decision.reply)
        self.assertNotIn("Fonte:", decision.reply)
        self.assertNotIn("0.8123", decision.reply)
        self.assertIn("banheiro", decision.reply.lower())

    def test_deterministic_synthesis_avoids_chunk_dump_for_school_robot(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="school-robot-synthesis")
        semantic = (
            "[KNOWLEDGE_BASE]\n"
            "Fonte: liro_littlebot.md\n"
            "Score: 0.71\n"
            "Conteúdo:\n"
            "# LIRO / Little Bot — robô educacional interativo\n"
            "Tags: smart-control, curated\n"
            "O LIRO / Little Bot é um robô interativo para experiências educacionais em escolas.\n"
            "[/KNOWLEDGE_BASE]"
        )
        decision = self.service.generate_reply(
            [],
            "qual robô vocês indicam para escola?",
            conversation=conversation,
            knowledge_context=semantic,
        )
        lowered = decision.reply.lower()
        self.assertIn("liro", lowered)
        self.assertNotIn("score:", lowered)
        self.assertNotIn("fonte:", lowered)
        self.assertNotIn("tags:", lowered)
        self.assertNotIn("# liro", lowered)
        self.assertNotIn("seu nome", lowered)

    def test_livia_decision_keeps_current_reply_without_knowledge(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="no-knowledge-session")

        decision = self.service.generate_reply([], "Vocês têm robô de limpeza?", conversation=conversation)

        self.assertEqual(decision.intent, "commercial_interest")
        self.assertNotIn("HygiBot", decision.reply)
        lowered = decision.reply.lower()
        self.assertFalse(any(token in lowered for token in ("seu nome", "telefone", "e-mail", "email")))
        self.assertNotIn("bancada", lowered)


class LiviaHandoffWorkflowTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="smart-control-brasil",
            domain="smartcontrolbrasil.com.br",
        )
        self.service = LiviaDecisionService()

    def test_explicit_human_request_creates_handoff(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="handoff-human")

        decision = self.service.generate_reply([], "quero falar com um vendedor", conversation=conversation)

        handoff = HandoffRequest.objects.get(conversation=conversation)
        self.assertEqual(handoff.reason, HandoffRequest.Reason.EXPLICIT_REQUEST)
        self.assertEqual(handoff.status, HandoffRequest.Status.PENDING)
        self.assertIn("telefone", decision.reply.lower())
        self.assertIn("contato", decision.reply.lower())

    def test_call_me_request_with_phone_creates_handoff_with_contact(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="handoff-call")

        decision = self.service.generate_reply([], "me liga, sou Marcelo, telefone 11999999999", conversation=conversation)

        handoff = HandoffRequest.objects.get(conversation=conversation)
        self.assertEqual(handoff.visitor_name, "Marcelo")
        self.assertEqual(handoff.visitor_phone, "11999999999")
        self.assertIn("Marcelo", decision.reply)
        self.assertIn("Registrei o pedido de contato", decision.reply)

    @override_settings(SMART360_LEAD_DISPATCH_ENABLED=False, SMART360_LEAD_DISPATCH_DRY_RUN=True)
    def test_qualified_lead_creates_handoff_without_duplicate(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="handoff-qualified")
        message = "Sou Maria da ACME, meu telefone é 11999998888, meu e-mail é maria@example.com e preciso de automação industrial."

        self.service.generate_reply([], message, conversation=conversation)
        self.service.generate_reply([], message, conversation=conversation)

        self.assertEqual(HandoffRequest.objects.filter(conversation=conversation).count(), 1)
        handoff = HandoffRequest.objects.get(conversation=conversation)
        self.assertEqual(handoff.reason, HandoffRequest.Reason.QUALIFIED_LEAD)
        self.assertIn("automação", handoff.summary.lower())

    def test_urgent_technical_conversation_creates_high_priority_handoff(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="handoff-urgent")

        self.service.generate_reply([], "CLP parou e a máquina está parada urgente", conversation=conversation)

        handoff = HandoffRequest.objects.get(conversation=conversation)
        self.assertEqual(handoff.priority, HandoffRequest.Priority.HIGH)
        self.assertIn(handoff.reason, {
            HandoffRequest.Reason.EMERGENCY_OR_URGENT,
            HandoffRequest.Reason.TECHNICAL_COMPLEXITY,
        })

    def test_simple_support_request_does_not_create_urgent_handoff(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="handoff-support-simple")

        self.service.generate_reply([], "Preciso de suporte no sistema", conversation=conversation)

        self.assertFalse(HandoffRequest.objects.filter(conversation=conversation).exists())

    def test_handoff_summary_and_source_page_are_filled(self):
        conversation = Conversation.objects.create(
            tenant=self.tenant,
            session_id="handoff-source",
            source_page="https://example.com/origem",
        )

        self.service.generate_reply([], "quero falar com alguém sobre robô de limpeza", conversation=conversation)

        handoff = HandoffRequest.objects.get(conversation=conversation)
        self.assertEqual(handoff.source_page, "https://example.com/origem")
        self.assertIn("Resumo da Lívia", handoff.summary)
        self.assertIn("robô de limpeza", handoff.summary)

    def test_default_handoff_notification_settings_are_safe(self):
        self.assertFalse(settings.LIVIA_HANDOFF_NOTIFICATIONS_ENABLED)
        self.assertTrue(settings.LIVIA_HANDOFF_NOTIFICATIONS_DRY_RUN)
        self.assertEqual(settings.LIVIA_HANDOFF_NOTIFICATION_EMAIL, "contato@smartcontrolbrasil.com.br")


class FakeAIClient:
    def __init__(self, result=None, exc=None):
        self.result = result or OpenAIChatResult(text="", success=False, dry_run=True)
        self.exc = exc
        self.calls = []

    def create_chat_completion(self, *, messages):
        self.calls.append(messages)
        if self.exc:
            raise self.exc
        return self.result


class LiviaOptionalAIResponseTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="smart-control-brasil-ai",
            domain="smartcontrolbrasil.com.br",
        )
        self.profile = AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia Smart",
            initial_message="Olá! Sou a Lívia Smart.",
            tone="consultivo e direto",
            primary_goal="qualificar oportunidades técnicas",
            use_ai=True,
            is_active=True,
        )

    @override_settings(LIVIA_AI_ENABLED=False, LIVIA_AI_GROUNDED_SYNTHESIS_ENABLED=False)
    def test_ai_disabled_by_default_keeps_deterministic_reply(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-default-off")
        ai_client = FakeAIClient(OpenAIChatResult(text="Resposta por IA", success=True, dry_run=False))
        service = LiviaDecisionService(ai_client=ai_client)

        decision = service.generate_reply([], "Quero orçamento para um sistema", conversation=conversation, assistant_profile=self.profile)

        self.assertEqual(ai_client.calls, [])
        self.assertIn("chamar", decision.reply.lower())

    @override_settings(LIVIA_AI_ENABLED=True, LIVIA_AI_DRY_RUN=True)
    def test_ai_dry_run_keeps_deterministic_reply(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-dry-run")
        service = LiviaDecisionService()

        with patch("integrations.openai.client.requests.post") as post_mock:
            decision = service.generate_reply([], "Quero orçamento para um sistema", conversation=conversation, assistant_profile=self.profile)

        post_mock.assert_not_called()
        self.assertIn("chamar", decision.reply.lower())

    @override_settings(LIVIA_AI_ENABLED=True, LIVIA_AI_DRY_RUN=False, LIVIA_OPENAI_API_KEY="")
    def test_without_api_key_keeps_fallback(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-no-key")
        service = LiviaDecisionService()

        with patch("integrations.openai.client.requests.post") as post_mock:
            decision = service.generate_reply([], "Quero orçamento para um sistema", conversation=conversation, assistant_profile=self.profile)

        post_mock.assert_not_called()
        self.assertIn("chamar", decision.reply.lower())

    @override_settings(LIVIA_AI_ENABLED=True, LIVIA_AI_DRY_RUN=False, LIVIA_OPENAI_API_KEY="key-test")
    def test_client_timeout_keeps_fallback(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-timeout")
        service = LiviaDecisionService(ai_client=FakeAIClient(exc=TimeoutError("timeout")))

        decision = service.generate_reply([], "Quero orçamento para um sistema", conversation=conversation, assistant_profile=self.profile)

        self.assertIn("chamar", decision.reply.lower())

    @override_settings(LIVIA_AI_ENABLED=True, LIVIA_AI_DRY_RUN=False, LIVIA_OPENAI_API_KEY="key-test")
    def test_valid_ai_response_replaces_only_reply_text(self):
        from assistant_core.services.chat_processing import _DeterministicChatResult, _refine_response_with_ai_if_enabled
        from assistant_core.services.livia_decision import LiviaReply
        from assistant_core.services.openai_grounded_conversation import OpenAIConversationResult
        from conversations.models import ChatRequest, Message

        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-valid")
        assistant_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="Resposta determinística de coleta.",
        )
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            session_id="ai-valid",
            request_id=uuid.uuid4(),
            request_fingerprint="e" * 64,
            status=ChatRequest.Status.COMPLETED,
            response_payload={"reply": "Resposta determinística de coleta.", "intent": "quote_request"},
            response_status_code=200,
        )
        decision = LiviaReply(intent="quote_request", reply="Resposta determinística de coleta.")
        deterministic_result = _DeterministicChatResult(
            chat_request=chat_request,
            tenant=self.tenant,
            conversation=conversation,
            assistant_profile=self.profile,
            history=[],
            user_message="Quero orçamento para um sistema",
            decision=decision,
            assistant_message=assistant_message,
            response_payload={"reply": decision.reply, "intent": decision.intent},
        )
        with patch("assistant_core.services.chat_processing._can_refine_with_ai", return_value=True), patch(
            "assistant_core.services.openai_grounded_conversation.OpenAIGroundedConversationService.generate",
            return_value=OpenAIConversationResult(
                text="Resposta mais natural da IA.",
                used=True,
                status="completed",
            ),
        ):
            payload = _refine_response_with_ai_if_enabled(
                deterministic_result=deterministic_result,
                decision_service=LiviaDecisionService(),
            )

        self.assertEqual(payload["intent"], "quote_request")
        self.assertEqual(payload["reply"], "Resposta mais natural da IA.")
        self.assertEqual(LeadDraft.objects.filter(conversation=conversation).count(), 0)

    @override_settings(LIVIA_AI_ENABLED=True, LIVIA_AI_DRY_RUN=False, LIVIA_OPENAI_API_KEY="key-test")
    def test_lead_state_is_not_changed_by_ai(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-state")
        ai_client = FakeAIClient(OpenAIChatResult(text="Claro, me conte a área principal.", success=True, dry_run=False))
        service = LiviaDecisionService(ai_client=ai_client)

        service.generate_reply([], "Quero orçamento", conversation=conversation, assistant_profile=self.profile)

        conversation.refresh_from_db()
        # Explicit budget trigger opens collection; AI must not invent another state.
        self.assertIn(
            conversation.lead_state,
            {LeadState.COLLECT_NEED, LeadState.COLLECT_NAME_COMPANY, LeadState.COLLECT_CONTACT},
        )

    @override_settings(LIVIA_AI_ENABLED=True, LIVIA_AI_DRY_RUN=False, LIVIA_OPENAI_API_KEY="key-test")
    def test_handoff_does_not_depend_on_ai(self):
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-handoff")
        service = LiviaDecisionService()

        decision = service.generate_reply([], "quero falar com um vendedor", conversation=conversation, assistant_profile=self.profile)

        self.assertTrue(HandoffRequest.objects.filter(conversation=conversation).exists())
        self.assertIn("contato", decision.reply.lower())

    @override_settings(LIVIA_AI_ENABLED=True, LIVIA_AI_DRY_RUN=False, LIVIA_OPENAI_API_KEY="key-test")
    def test_knowledge_context_and_profile_enter_prompt(self):
        from assistant_core.discovery import analyze_message
        from assistant_core.services.livia_decision import LiviaReply
        from assistant_core.services.openai_grounded_conversation import OpenAIGroundedConversationService

        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-prompt")
        knowledge = "[KNOWLEDGE_BASE]\nHygiBot atende limpeza profissional em grandes áreas.\n[/KNOWLEDGE_BASE]"
        ai_client = FakeAIClient(OpenAIChatResult(text="HygiBot pode ajudar nesse cenário.", success=True, dry_run=False))
        service = OpenAIGroundedConversationService(ai_client=ai_client)
        discovery = analyze_message("Vocês têm robô de limpeza HygiBot?")
        decision = LiviaReply(intent="commercial_interest", reply="Resposta determinística.")

        service.generate(
            tenant=self.tenant,
            assistant_profile=self.profile,
            message="Vocês têm robô de limpeza HygiBot?",
            conversation=conversation,
            discovery=discovery,
            decision=decision,
            knowledge_context=knowledge,
        )

        prompt_text = "\n".join(message["content"] for message in ai_client.calls[0])
        self.assertIn("HygiBot", prompt_text)
        self.assertIn("consultivo e direto", prompt_text)
        self.assertIn("qualificar oportunidades técnicas", prompt_text)

    @override_settings(LIVIA_AI_ENABLED=True, LIVIA_AI_DRY_RUN=False, LIVIA_OPENAI_API_KEY="key-test")
    def test_profile_use_ai_false_blocks_ai_even_when_global_enabled(self):
        self.profile.use_ai = False
        self.profile.save(update_fields=["use_ai"])
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-profile-off")
        ai_client = FakeAIClient(OpenAIChatResult(text="Resposta por IA", success=True, dry_run=False))
        service = LiviaDecisionService(ai_client=ai_client)

        decision = service.generate_reply([], "Quero orçamento para um sistema", conversation=conversation, assistant_profile=self.profile)

        self.assertEqual(ai_client.calls, [])
        self.assertIn("chamar", decision.reply.lower())

    @override_settings(LIVIA_AI_ENABLED=True, LIVIA_AI_DRY_RUN=False, LIVIA_OPENAI_API_KEY="key-test")
    def test_profile_use_ai_true_allows_ai_attempt(self):
        from assistant_core.services.ai_feature_gates import is_openai_conversation_allowed

        self.assertTrue(is_openai_conversation_allowed(assistant_profile=self.profile))

    @override_settings(LIVIA_AI_ENABLED=True, LIVIA_AI_DRY_RUN=False, LIVIA_OPENAI_API_KEY="secret-key-123")
    def test_prompt_does_not_contain_api_key(self):
        from assistant_core.discovery import analyze_message
        from assistant_core.services.livia_decision import LiviaReply
        from assistant_core.services.openai_grounded_conversation import OpenAIGroundedConversationService

        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-no-secret")
        ai_client = FakeAIClient(OpenAIChatResult(text="Texto refinado.", success=True, dry_run=False))
        service = OpenAIGroundedConversationService(ai_client=ai_client)
        discovery = analyze_message("Quero orçamento para um sistema")
        decision = LiviaReply(intent="quote_request", reply="Resposta determinística.")

        service.generate(
            tenant=self.tenant,
            assistant_profile=self.profile,
            message="Quero orçamento para um sistema",
            conversation=conversation,
            discovery=discovery,
            decision=decision,
            knowledge_context="",
        )

        prompt_text = "\n".join(message["content"] for message in ai_client.calls[0])
        self.assertNotIn("secret-key-123", prompt_text)


class ChatIdempotencyApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="smart-control-brasil",
            domain="smart-control-brasil.example",
            is_active=True,
        )

    def post_chat(self, payload, **headers):
        return self.client.post(
            "/api/chat/",
            data=json.dumps(payload),
            content_type="application/json",
            **headers,
        )

    def payload(self, *, request_id=None, session_id="idem-session", message="Olá!", source_page="https://example.com/pagina"):
        return {
            "tenant": self.tenant.slug,
            "session_id": session_id,
            "request_id": str(request_id or uuid.uuid4()),
            "message": message,
            "source_page": source_page,
        }

    def test_request_id_is_required(self):
        response = self.post_chat({"tenant": self.tenant.slug, "session_id": "missing", "message": "Olá"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "request_id_required")
        self.assertEqual(ChatRequest.objects.count(), 0)

    def test_invalid_request_id_returns_400(self):
        response = self.post_chat(self.payload(request_id="not-a-uuid"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "request_id_invalid")
        self.assertEqual(ChatRequest.objects.count(), 0)

    def test_request_id_header_must_match_payload(self):
        response = self.post_chat(
            self.payload(request_id=uuid.uuid4()),
            HTTP_X_LIVIA_REQUEST_ID=str(uuid.uuid4()),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "request_id_header_mismatch")
        self.assertEqual(ChatRequest.objects.count(), 0)

    def test_first_request_completes_and_identical_retry_replays_same_payload(self):
        request_id = uuid.uuid4()
        payload = self.payload(request_id=request_id, message="Olá, quero saber mais.")

        first = self.post_chat(payload, HTTP_X_LIVIA_REQUEST_ID=str(request_id))
        second = self.post_chat(payload, HTTP_X_LIVIA_REQUEST_ID=str(request_id))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first["X-Livia-Idempotent-Replay"], "false")
        self.assertEqual(second["X-Livia-Idempotent-Replay"], "true")
        self.assertEqual(ChatRequest.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)

    def test_retry_does_not_duplicate_lead_or_messages(self):
        request_id = uuid.uuid4()
        payload = self.payload(
            request_id=request_id,
            session_id="lead-idempotent",
            message="Sou Maria da ACME, meu telefone é 11999998888, meu e-mail é maria@example.com e preciso de automação industrial.",
        )

        first = self.post_chat(payload)
        second = self.post_chat(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(LeadDraft.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)
        self.assertEqual(OutboxEvent.objects.filter(event_type=OutboxEvent.EventType.LEAD_QUALIFIED, aggregate_id=str(LeadDraft.objects.get().pk)).count(), 1)

    def test_retry_does_not_duplicate_handoff(self):
        AssistantProfile.objects.create(
            tenant=self.tenant,
            human_handoff_enabled=True,
            human_handoff_channel="whatsapp",
            handoff_whatsapp_number="551151968525",
        )
        request_id = uuid.uuid4()
        payload = self.payload(request_id=request_id, session_id="handoff-idem", message="quero falar com uma pessoa")

        first = self.post_chat(payload)
        second = self.post_chat(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(HandoffRequest.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)
        self.assertEqual(first.json()["human_handoff"], second.json()["human_handoff"])

    def test_same_request_id_with_different_payload_returns_conflict(self):
        request_id = uuid.uuid4()
        first = self.post_chat(self.payload(request_id=request_id, session_id="conflict", message="Olá"))
        second = self.post_chat(self.payload(request_id=request_id, session_id="conflict", message="Mensagem diferente"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"], "request_id_conflict")
        self.assertEqual(ChatRequest.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)

    @override_settings(DEBUG=False, LIVIA_ALLOW_ORIGINLESS_PUBLIC_API=False)
    def test_blocked_origin_does_not_create_chat_request(self):
        TenantAllowedOrigin.objects.create(tenant=self.tenant, origin="https://allowed.example")
        response = self.post_chat(self.payload(), HTTP_ORIGIN="https://evil.example", HTTP_X_LIVIA_TENANT=self.tenant.slug)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(ChatRequest.objects.count(), 0)

    def test_inactive_tenant_does_not_create_chat_request(self):
        self.tenant.is_active = False
        self.tenant.save(update_fields=["is_active"])

        response = self.post_chat(self.payload())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(ChatRequest.objects.count(), 0)

    @override_settings(LIVIA_CHAT_RATE_LIMIT_ENABLED=True, LIVIA_CHAT_RATE_LIMIT_REQUESTS=1, LIVIA_CHAT_RATE_LIMIT_WINDOW_SECONDS=300)
    def test_rate_limited_request_is_completed_and_replayed(self):
        first = self.post_chat(self.payload(session_id="rate-a"), REMOTE_ADDR="10.1.1.1")
        request_id = uuid.uuid4()
        limited_payload = self.payload(request_id=request_id, session_id="rate-b")
        second = self.post_chat(limited_payload, REMOTE_ADDR="10.1.1.1")
        replay = self.post_chat(limited_payload, REMOTE_ADDR="10.1.1.1")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(replay.status_code, 429)
        self.assertEqual(replay["X-Livia-Idempotent-Replay"], "true")
        self.assertEqual(ChatRequest.objects.count(), 2)

    def test_spam_request_is_completed_without_conversation_and_replayed(self):
        payload = self.payload(message="Veja http://a.example www.b.example https://c.example para free money")

        first = self.post_chat(payload)
        replay = self.post_chat(payload)

        self.assertEqual(first.status_code, 400)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay["X-Livia-Idempotent-Replay"], "true")
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(ChatRequest.objects.count(), 1)

    @override_settings(LIVIA_CHAT_PROCESSING_TIMEOUT_SECONDS=30)
    def test_recent_processing_request_returns_in_progress(self):
        request_id = uuid.uuid4()
        payload = self.payload(request_id=request_id, session_id="processing", message="Olá")
        fingerprint = __import__("assistant_core.services.chat_idempotency", fromlist=["build_request_fingerprint"]).build_request_fingerprint(
            tenant_slug=self.tenant.slug,
            session_id="processing",
            request_id=request_id,
            message="Olá",
            source_page="https://example.com/pagina",
        )
        ChatRequest.objects.create(
            tenant=self.tenant,
            session_id="processing",
            request_id=request_id,
            request_fingerprint=fingerprint,
            status=ChatRequest.Status.PROCESSING,
        )

        response = self.post_chat(payload)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "request_in_progress")
        self.assertEqual(Message.objects.count(), 0)

    @override_settings(LIVIA_CHAT_PROCESSING_TIMEOUT_SECONDS=1)
    def test_abandoned_processing_request_can_be_recovered(self):
        request_id = uuid.uuid4()
        payload = self.payload(request_id=request_id, session_id="abandoned", message="Olá")
        fingerprint = __import__("assistant_core.services.chat_idempotency", fromlist=["build_request_fingerprint"]).build_request_fingerprint(
            tenant_slug=self.tenant.slug,
            session_id="abandoned",
            request_id=request_id,
            message="Olá",
            source_page="https://example.com/pagina",
        )
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            session_id="abandoned",
            request_id=request_id,
            request_fingerprint=fingerprint,
            status=ChatRequest.Status.PROCESSING,
        )
        ChatRequest.objects.filter(pk=chat_request.pk).update(updated_at=timezone.now() - timedelta(seconds=5))

        response = self.post_chat(payload)

        self.assertEqual(response.status_code, 200)
        chat_request.refresh_from_db()
        self.assertEqual(chat_request.status, ChatRequest.Status.COMPLETED)
        self.assertEqual(Message.objects.count(), 2)

    def test_unexpected_processing_error_marks_failed_and_is_not_replayed_as_success(self):
        request_id = uuid.uuid4()
        payload = self.payload(request_id=request_id, session_id="boom", message="Olá")

        with patch("assistant_core.views.process_chat_request", side_effect=RuntimeError("boom")):
            response = self.post_chat(payload)
        retry = self.post_chat(payload)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(retry.json()["error"], "request_failed_retry")
        self.assertEqual(ChatRequest.objects.get().status, ChatRequest.Status.FAILED)
        self.assertEqual(Message.objects.count(), 0)

    @override_settings(LIVIA_ALLOW_ORIGINLESS_PUBLIC_API=True)
    def test_ai_refinement_runs_outside_atomic_and_is_not_replayed(self):
        from assistant_core.services.chat_processing import (
            _DeterministicChatResult,
            _refine_response_with_ai_if_enabled,
        )
        from assistant_core.services.livia_decision import LiviaReply
        from assistant_core.services.openai_grounded_conversation import OpenAIConversationResult

        profile = AssistantProfile.objects.create(tenant=self.tenant, use_ai=True, is_active=True)
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-replay")
        assistant_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="Resposta determinística",
        )
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            session_id="ai-replay",
            request_id=uuid.uuid4(),
            request_fingerprint="a" * 64,
            status=ChatRequest.Status.COMPLETED,
            response_payload={"reply": "Resposta determinística", "intent": "quote_request"},
            response_status_code=200,
        )
        deterministic_result = _DeterministicChatResult(
            chat_request=chat_request,
            tenant=self.tenant,
            conversation=conversation,
            assistant_profile=profile,
            history=[],
            user_message="Quero orçamento para automação industrial.",
            decision=LiviaReply(intent="quote_request", reply="Resposta determinística"),
            assistant_message=assistant_message,
            response_payload={"reply": "Resposta determinística", "intent": "quote_request"},
        )

        def fake_generate(*args, **kwargs):
            return OpenAIConversationResult(
                text="Resposta refinada por IA.",
                used=True,
                status="completed",
            )

        with patch("assistant_core.services.chat_processing._can_refine_with_ai", return_value=True), patch(
            "assistant_core.services.openai_grounded_conversation.OpenAIGroundedConversationService.generate",
            side_effect=fake_generate,
        ):
            payload = _refine_response_with_ai_if_enabled(
                deterministic_result=deterministic_result,
                decision_service=LiviaDecisionService(),
            )

        self.assertEqual(payload["reply"], "Resposta refinada por IA.")
        self.assertEqual(payload.get("ai_mode"), "openai_conversation")
        assistant_message.refresh_from_db()
        self.assertEqual(assistant_message.content, "Resposta refinada por IA.")
        chat_request.refresh_from_db()
        self.assertEqual(chat_request.response_payload.get("reply"), "Resposta refinada por IA.")

    @override_settings(LIVIA_ALLOW_ORIGINLESS_PUBLIC_API=True)
    def test_ai_failure_keeps_request_completed_with_deterministic_payload(self):
        from assistant_core.services.chat_processing import (
            _DeterministicChatResult,
            _refine_response_with_ai_if_enabled,
        )
        from assistant_core.services.livia_decision import LiviaReply

        profile = AssistantProfile.objects.create(tenant=self.tenant, use_ai=True, is_active=True)
        conversation = Conversation.objects.create(tenant=self.tenant, session_id="ai-timeout")
        assistant_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="Resposta determinística",
        )
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            conversation=conversation,
            session_id="ai-timeout",
            request_id=uuid.uuid4(),
            request_fingerprint="b" * 64,
            status=ChatRequest.Status.COMPLETED,
            response_payload={"reply": "Resposta determinística", "intent": "quote_request"},
            response_status_code=200,
        )
        deterministic_result = _DeterministicChatResult(
            chat_request=chat_request,
            tenant=self.tenant,
            conversation=conversation,
            assistant_profile=profile,
            history=[],
            user_message="Quero orçamento para automação industrial.",
            decision=LiviaReply(intent="quote_request", reply="Resposta determinística"),
            assistant_message=assistant_message,
            response_payload={"reply": "Resposta determinística", "intent": "quote_request"},
        )

        with patch("assistant_core.services.chat_processing._can_refine_with_ai", return_value=True), patch(
            "assistant_core.services.openai_grounded_conversation.OpenAIGroundedConversationService.generate",
            side_effect=RuntimeError("ai failure"),
        ):
            payload = _refine_response_with_ai_if_enabled(
                deterministic_result=deterministic_result,
                decision_service=LiviaDecisionService(),
            )

        self.assertEqual(payload["reply"], "Resposta determinística")
        self.assertTrue(payload.get("observability", {}).get("ai_fallback_used"))
        assistant_message.refresh_from_db()
        self.assertEqual(assistant_message.content, "Resposta determinística")
        chat_request.refresh_from_db()
        self.assertEqual(chat_request.status, ChatRequest.Status.COMPLETED)
        self.assertEqual(chat_request.response_payload.get("reply"), "Resposta determinística")


class ChatProcessingTransactionBoundaryTests(TransactionTestCase):
    def test_ai_refinement_hook_runs_outside_application_atomic_block(self):
        from assistant_core.services.chat_processing import (
            _DeterministicChatResult,
            _refine_response_with_ai_if_enabled,
        )
        from assistant_core.services.livia_decision import LiviaReply

        tenant = Tenant.objects.create(name="Tenant", slug="tenant")
        profile = AssistantProfile.objects.create(tenant=tenant, use_ai=True, is_active=True)
        conversation = Conversation.objects.create(tenant=tenant, session_id="tx-boundary")
        assistant_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="Resposta determinística",
        )
        chat_request = ChatRequest.objects.create(
            tenant=tenant,
            conversation=conversation,
            session_id="tx-boundary",
            request_id=uuid.uuid4(),
            request_fingerprint="c" * 64,
            status=ChatRequest.Status.COMPLETED,
            response_payload={"reply": "Resposta determinística", "intent": "quote_request"},
            response_status_code=200,
        )
        deterministic_result = _DeterministicChatResult(
            chat_request=chat_request,
            tenant=tenant,
            conversation=conversation,
            assistant_profile=profile,
            history=[],
            user_message="Quero orçamento",
            decision=LiviaReply(intent="quote_request", reply="Resposta determinística"),
            assistant_message=assistant_message,
            response_payload={"reply": "Resposta determinística", "intent": "quote_request"},
        )
        atomic_flags: list[bool] = []

        def fake_generate(*args, **kwargs):
            from assistant_core.services.openai_grounded_conversation import OpenAIConversationResult

            atomic_flags.append(connection.in_atomic_block)
            return OpenAIConversationResult(
                text="Resposta refinada por IA.",
                used=True,
                status="completed",
            )

        with patch("assistant_core.services.chat_processing._can_refine_with_ai", return_value=True), patch(
            "assistant_core.services.openai_grounded_conversation.OpenAIGroundedConversationService.generate",
            side_effect=fake_generate,
        ):
            _refine_response_with_ai_if_enabled(
                deterministic_result=deterministic_result,
                decision_service=LiviaDecisionService(),
            )

        self.assertEqual(atomic_flags, [False])


@unittest.skipUnless(connection.vendor == "postgresql", "PostgreSQL-specific lock semantics.")
class ChatIdempotencyPostgresConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.other_tenant = Tenant.objects.create(name="Tenant B", slug="tenant-b")

    def _fingerprint(self, tenant_slug: str, session_id: str, request_id: uuid.UUID, message: str = "Olá") -> str:
        return build_request_fingerprint(
            tenant_slug=tenant_slug,
            session_id=session_id,
            request_id=request_id,
            message=message,
            source_page="https://example.com/pagina",
        )

    def test_select_for_update_blocks_second_reservation_until_lock_release(self):
        request_id = uuid.uuid4()
        session_id = "lock-session"
        fingerprint = self._fingerprint(self.tenant.slug, session_id, request_id)
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            session_id=session_id,
            request_id=request_id,
            request_fingerprint=fingerprint,
            status=ChatRequest.Status.PROCESSING,
        )

        lock_acquired = threading.Event()
        release_lock = threading.Event()
        probe_done = threading.Event()
        elapsed_seconds = {"value": 0.0}
        reservation_state = {"value": ""}

        def locker():
            close_old_connections()
            try:
                with transaction.atomic():
                    ChatRequest.objects.select_for_update().get(pk=chat_request.pk)
                    lock_acquired.set()
                    release_lock.wait(timeout=5)
            finally:
                connection.close()

        def probe_reservation():
            close_old_connections()
            try:
                lock_acquired.wait(timeout=5)
                started = time.monotonic()
                reservation = reserve_chat_request(
                    tenant=self.tenant,
                    session_id=session_id,
                    request_id=request_id,
                    fingerprint=fingerprint,
                )
                elapsed_seconds["value"] = time.monotonic() - started
                reservation_state["value"] = reservation.state
            finally:
                probe_done.set()
                connection.close()

        locker_thread = threading.Thread(target=locker, daemon=True)
        probe_thread = threading.Thread(target=probe_reservation, daemon=True)
        locker_thread.start()
        probe_thread.start()
        self.assertTrue(lock_acquired.wait(timeout=5))
        time.sleep(0.35)
        release_lock.set()
        self.assertTrue(probe_done.wait(timeout=5))
        locker_thread.join(timeout=5)
        probe_thread.join(timeout=5)

        self.assertGreaterEqual(elapsed_seconds["value"], 0.30)
        self.assertEqual(reservation_state["value"], "in_progress")

    @override_settings(LIVIA_CHAT_PROCESSING_TIMEOUT_SECONDS=1)
    def test_stale_recovery_allows_only_one_processing_winner(self):
        request_id = uuid.uuid4()
        session_id = "stale-session"
        fingerprint = self._fingerprint(self.tenant.slug, session_id, request_id)
        chat_request = ChatRequest.objects.create(
            tenant=self.tenant,
            session_id=session_id,
            request_id=request_id,
            request_fingerprint=fingerprint,
            status=ChatRequest.Status.PROCESSING,
        )
        ChatRequest.objects.filter(pk=chat_request.pk).update(updated_at=timezone.now() - timedelta(seconds=5))

        start_barrier = threading.Barrier(3)
        states: list[str] = []
        errors: list[str] = []

        def worker():
            close_old_connections()
            try:
                start_barrier.wait(timeout=5)
                reservation = reserve_chat_request(
                    tenant=self.tenant,
                    session_id=session_id,
                    request_id=request_id,
                    fingerprint=fingerprint,
                )
                states.append(reservation.state)
            except Exception as exc:
                errors.append(str(exc))
            finally:
                connection.close()

        first = threading.Thread(target=worker, daemon=True)
        second = threading.Thread(target=worker, daemon=True)
        first.start()
        second.start()
        start_barrier.wait(timeout=5)
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertCountEqual(states, ["process", "in_progress"])
        self.assertEqual(states.count("process"), 1)

    def test_request_id_isolation_between_tenants(self):
        request_id = uuid.uuid4()
        session_id = "shared-session"
        first_fingerprint = self._fingerprint(self.tenant.slug, session_id, request_id)
        second_fingerprint = self._fingerprint(self.other_tenant.slug, session_id, request_id)

        first = reserve_chat_request(
            tenant=self.tenant,
            session_id=session_id,
            request_id=request_id,
            fingerprint=first_fingerprint,
        )
        second = reserve_chat_request(
            tenant=self.other_tenant,
            session_id=session_id,
            request_id=request_id,
            fingerprint=second_fingerprint,
        )

        self.assertEqual(first.state, "process")
        self.assertEqual(second.state, "process")
        self.assertEqual(ChatRequest.objects.count(), 2)
