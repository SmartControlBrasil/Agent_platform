from __future__ import annotations

import json
import uuid

from django.core.cache import cache
from django.test import TestCase, override_settings

from conversations.models import Conversation
from knowledge_base.models import KnowledgeDocument
from knowledge_base.rag.content_classification import classify_rag_source, is_policy_leak_text
from leads.models import LeadDraft
from tenants.models import AssistantProfile, Tenant, TenantAllowedOrigin


FORBIDDEN_SNIPPETS = (
    "catálogo maior",
    "catalogo maior",
    "python",
    "crianças e jovens",
    "criancas e jovens",
    "não deve prometer",
    "nao deve prometer",
    "telefone/whatsapp",
    "seu telefone",
    "me passa seu telefone",
)


@override_settings(
    ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"],
    LIVIA_AI_ENABLED=False,
    LIVIA_CHAT_RATE_LIMIT_ENABLED=False,
    LIVIA_LEAD_NOTIFICATIONS_ENABLED=False,
    LIVIA_RAG_ENABLED=False,
)
class DunoConversationalQualityTests(TestCase):
    """Regressão da conversa real SCB (robô de limpeza → Duno)."""

    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(
            name="Smart Control Brasil",
            slug="smart-control-brasil",
            domain="https://www.smartcontrolbrasil.com.br",
            is_active=True,
        )
        TenantAllowedOrigin.objects.create(tenant=self.tenant, origin="https://www.smartcontrolbrasil.com.br")
        AssistantProfile.objects.create(
            tenant=self.tenant,
            name="Lívia",
            initial_message="Olá, eu sou a Lívia da Smart Control Brasil. Posso te ajudar com automação, robótica, manutenção técnica ou sistemas web.",
            business_domain="robótica, automação e sistemas",
            short_description="Atendimento consultivo Smart Control Brasil",
            notification_email="comercial@smartcontrolbrasil.com.br",
        )
        KnowledgeDocument.objects.create(
            tenant=self.tenant,
            title="HygiBot / Dune Bot — robô de limpeza",
            slug="hygibot-dune",
            content=(
                "Nome oficial: HygiBot / Dune Bot. Também referido como Duno. "
                "Apoiar rotinas de limpeza em grandes áreas, com modos de lavar, varrer, aspirar e passar pano "
                "conforme ambiente e operação. Ambientes: shoppings, indústrias, hospitais e grandes áreas."
            ),
            tags=["robotics", "xyron", "hygibot", "duno", "dune", "limpeza"],
            status=KnowledgeDocument.Status.ACTIVE,
        )
        KnowledgeDocument.objects.create(
            tenant=self.tenant,
            title="Sistemas Python e Web",
            slug="sistemas-python-web",
            content=(
                "Desenvolvemos sistemas, integrações, IoT e soluções digitais em Python. "
                "Lojas virtuais e catálogos de produtos fazem parte do portfólio de software."
            ),
            tags=["software_web", "python", "site"],
            status=KnowledgeDocument.Status.ACTIVE,
        )
        KnowledgeDocument.objects.create(
            tenant=self.tenant,
            title="Robô educacional interativo",
            slug="robo-educacional",
            content=(
                "Robô interativo para aproximar crianças e jovens da tecnologia por meio de "
                "experiências educacionais com comunicação, movimento e interação."
            ),
            tags=["robotics", "educational", "liro"],
            status=KnowledgeDocument.Status.ACTIVE,
        )
        KnowledgeDocument.objects.create(
            tenant=self.tenant,
            title="09_LIMITES_E_NAO_PROMETER",
            slug="limites-nao-prometer",
            content=(
                "A Lívia da Smart Control Brasil NÃO deve prometer sem dado configurado: "
                "preço fechado; prazo de entrega; SLA; estoque. Catálogo oficial apenas como backing."
            ),
            tags=["policy", "internal", "limites"],
            status=KnowledgeDocument.Status.ACTIVE,
        )
        self.session_id = f"duno-{uuid.uuid4().hex[:8]}"

    def _chat(self, message: str) -> dict:
        rid = str(uuid.uuid4())
        response = self.client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "tenant": self.tenant.slug,
                    "session_id": self.session_id,
                    "request_id": rid,
                    "message": message,
                    "source_page": "https://www.smartcontrolbrasil.com.br/xyron/",
                }
            ),
            content_type="application/json",
            HTTP_ORIGIN="https://www.smartcontrolbrasil.com.br",
            HTTP_X_LIVIA_REQUEST_ID=rid,
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _assert_clean_reply(self, reply: str):
        lowered = reply.lower()
        for snippet in FORBIDDEN_SNIPPETS:
            self.assertNotIn(snippet, lowered, reply)
        self.assertFalse(is_policy_leak_text(reply), reply)

    def test_full_duno_conversation_keeps_domain_entity_and_no_collection(self):
        turns = [
            "Oi gostaria de um robo de limpeza",
            "me fale mais sobre ele",
            "eu vi o duno",
            "não quero passar contato agora, tire minhas duvidas",
            "fale sobre o duno",
        ]
        replies = []
        for message in turns:
            payload = self._chat(message)
            replies.append(payload["reply"])
            self._assert_clean_reply(payload["reply"])
            # Nenhuma coleta prematura.
            lowered = payload["reply"].lower()
            self.assertNotIn("qual é o seu nome", lowered)
            self.assertNotIn("nome da empresa", lowered)

        # Última resposta deve falar de limpeza / Duno e não misturar Python/educacional.
        last = replies[-1].lower()
        self.assertTrue(
            any(token in last for token in ("limpeza", "lavar", "varrer", "aspirar", "duno", "áreas", "areas")),
            replies[-1],
        )

        conversation = Conversation.objects.get(tenant=self.tenant, session_id=self.session_id)
        lead = LeadDraft.objects.filter(conversation=conversation).first()
        self.assertIsNotNone(lead)
        data = lead.qualification_data or {}
        self.assertEqual(data.get("active_entity"), "Duno")
        self.assertEqual(data.get("active_domain"), "robotics")
        self.assertTrue(data.get("contact_collection_deferred") or data.get("collection_paused"))

        # Orçamento depois ativa coleta.
        price = self._chat("qual o valor?")
        self._assert_clean_reply(price["reply"])
        self.assertNotIn("telefone/whatsapp", price["reply"].lower())

        budget = self._chat("quero um orçamento desse robô")
        budget_lower = budget["reply"].lower()
        self.assertTrue(
            any(token in budget_lower for token in ("chamar", "nome", "telefone", "e-mail", "email", "whatsapp", "empresa")),
            budget["reply"],
        )

        refuse = self._chat("prefiro não passar meu telefone agora")
        refuse_lower = refuse["reply"].lower()
        self.assertNotIn("me passa seu telefone", refuse_lower)
        self.assertNotIn("telefone/whatsapp ou e-mail", refuse_lower)

    def test_internal_policy_never_leaks_to_reply(self):
        classification = classify_rag_source(
            source_name="09_LIMITES_E_NAO_PROMETER",
            source_reference="09_limites/limites_e_nao_prometer.md",
            text="A Lívia NÃO deve prometer preço",
        )
        self.assertFalse(classification.is_answerable)
        self.assertEqual(classification.visibility, "internal")

        payload = self._chat("fale sobre o duno")
        self._assert_clean_reply(payload["reply"])

    def test_ecommerce_followup_not_used_for_robotics(self):
        payload = self._chat("gostaria de um robô de limpeza para shopping")
        self.assertNotIn("catálogo maior", payload["reply"].lower())
        self.assertNotIn("catalogo maior", payload["reply"].lower())

    def test_topic_switch_to_python_changes_domain(self):
        self._chat("quero saber do Duno")
        self._chat("fale sobre o duno")
        payload = self._chat("agora me fale sobre sistemas em Python")
        lowered = payload["reply"].lower()
        # Pode falar de sistemas/python; não deve insistir em limpeza/Duno como foco obrigatório.
        conversation = Conversation.objects.get(tenant=self.tenant, session_id=self.session_id)
        lead = LeadDraft.objects.filter(conversation=conversation).first()
        data = (lead.qualification_data if lead else {}) or {}
        self.assertEqual(data.get("active_domain"), "software_web")
        self.assertNotIn("catálogo maior", lowered)


@override_settings(
    ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"],
    LIVIA_AI_ENABLED=False,
    LIVIA_CHAT_RATE_LIMIT_ENABLED=False,
    LIVIA_LEAD_NOTIFICATIONS_ENABLED=False,
)
class PitondoRegressionAfterDunoFixTests(TestCase):
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
            initial_message="Olá! Sou a Lívia da Granimármores Pitondo.",
            business_domain="marmoraria",
            short_description="Bancadas e pedras naturais",
            notification_email="contato@granimarmorespitondo.com.br",
        )
        KnowledgeDocument.objects.create(
            tenant=self.tenant,
            title="Bancadas de cozinha",
            slug="bancadas-cozinha",
            content=(
                "A Granimármores Pitondo desenvolve bancadas de cozinha sob medida em granito e mármore. "
                "É possível prever recortes para cooktop e pia conforme o projeto."
            ),
            tags=["cozinha", "bancada", "cooktop", "granito"],
        )
        self.session_id = f"pit-{uuid.uuid4().hex[:8]}"

    def _chat(self, message: str) -> dict:
        rid = str(uuid.uuid4())
        response = self.client.post(
            "/api/chat/",
            data=json.dumps(
                {
                    "tenant": self.tenant.slug,
                    "session_id": self.session_id,
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

    def test_pitondo_kitchen_flow_no_scb_leak(self):
        replies = []
        for message in (
            "quero uma bancada",
            "é para cozinha",
            "vou usar cooktop",
            "qual material vocês recomendam?",
            "me fale mais sobre ele",
        ):
            payload = self._chat(message)
            replies.append(payload["reply"])
            lowered = payload["reply"].lower()
            self.assertNotIn("duno", lowered)
            self.assertNotIn("python", lowered)
            self.assertNotIn("mitsubishi", lowered)
            self.assertNotIn("catálogo maior", lowered)
        joined = " ".join(replies).lower()
        self.assertTrue(any(token in joined for token in ("bancada", "cozinha", "granito", "mármore", "marmore", "cooktop")))
