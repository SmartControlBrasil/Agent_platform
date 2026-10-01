"""Fase 16B.2 — classificação semântica de intenção comercial (mock, sem OpenAI real)."""

from __future__ import annotations

import json
import uuid
from unittest import mock

from django.test import TestCase, override_settings

from assistant_core.consultative_policy import (
    COLLECTION_ACTIVE_KEY,
    CollectionTrigger,
    decide_collection,
    detect_collection_trigger,
)
from assistant_core.dialogue_memory import COMMERCIAL_INTENT_KEY
from assistant_core.services.livia_decision import LiviaDecisionService
from assistant_core.services.semantic_commercial_intent import (
    CommercialIntentLabel,
    SemanticCommercialIntentDecision,
    SemanticCommercialIntentService,
    SuggestedCommercialAction,
    INTENT_SOURCE_SEMANTIC,
    parse_semantic_classifier_payload,
)
from conversations.models import Conversation
from leads.models import LeadDraft
from tenants.models import Tenant


SEMANTIC_SETTINGS = dict(
    LIVIA_AI_ENABLED=True,
    LIVIA_AI_DRY_RUN=False,
    LIVIA_OPENAI_API_KEY="test-key",
    LIVIA_SEMANTIC_INTENT_ENABLED=True,
    LIVIA_RAG_ENABLED=False,
    LIVIA_AI_GROUNDED_SYNTHESIS_ENABLED=False,
)


def _decision(
    *,
    intent=CommercialIntentLabel.COMMERCIAL_INTEREST,
    commercial_intent=True,
    confidence=0.9,
    suggested_action=SuggestedCommercialAction.START_COLLECTION,
    reason="Necessidade concreta do visitante.",
):
    return SemanticCommercialIntentDecision(
        intent=intent,
        commercial_intent=commercial_intent,
        confidence=confidence,
        reason=reason,
        suggested_action=suggested_action,
        available=True,
    )


class FakeSemanticClassifier:
    def __init__(self, decision: SemanticCommercialIntentDecision | None = None):
        self.decision = decision or _decision()
        self.calls = 0
        self.last_kwargs: dict = {}

    def classify(self, **kwargs):
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        return self.decision

    def should_act_on(self, decision):
        return SemanticCommercialIntentService().should_act_on(decision)


@override_settings(**SEMANTIC_SETTINGS)
class SemanticCommercialIntentPolicyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Semantic", slug="semantic-intent")
        self.conversation = Conversation.objects.create(tenant=self.tenant, session_id=str(uuid.uuid4()))

    def test_a_technical_question_no_collection(self):
        fake = FakeSemanticClassifier(
            _decision(
                intent=CommercialIntentLabel.TECHNICAL_QUESTION,
                commercial_intent=False,
                suggested_action=SuggestedCommercialAction.CONTINUE_CONVERSATION,
                reason="Pergunta conceitual.",
            )
        )
        gate = decide_collection(
            current_message="Como funciona um CLP?",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertFalse(gate.should_collect)
        self.assertEqual(fake.calls, 1)

    def test_b_institutional_no_lead(self):
        fake = FakeSemanticClassifier(
            _decision(
                intent=CommercialIntentLabel.OTHER,
                commercial_intent=False,
                suggested_action=SuggestedCommercialAction.CONTINUE_CONVERSATION,
                reason="Pergunta institucional.",
            )
        )
        gate = decide_collection(
            current_message="Vocês trabalham com Mitsubishi?",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertFalse(gate.should_collect)

    def test_c_concrete_need_starts_collection(self):
        fake = FakeSemanticClassifier()
        gate = decide_collection(
            current_message="Tenho três máquinas e preciso automatizá-las.",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertTrue(gate.should_collect)
        self.assertEqual(gate.intent_source, INTENT_SOURCE_SEMANTIC)
        self.assertEqual(gate.trigger, CollectionTrigger.BUDGET)

    def test_d_supplier_search(self):
        fake = FakeSemanticClassifier()
        gate = decide_collection(
            current_message="Estamos procurando uma empresa para automatizar nossa fábrica.",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertTrue(gate.should_collect)

    def test_e_proposal_without_budget_word(self):
        fake = FakeSemanticClassifier(
            _decision(
                intent=CommercialIntentLabel.QUOTE_REQUEST,
                commercial_intent=True,
                suggested_action=SuggestedCommercialAction.START_COLLECTION,
                reason="Pedido de proposta.",
            )
        )
        gate = decide_collection(
            current_message="Vocês conseguem avaliar isso e me passar uma proposta?",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertTrue(gate.should_collect)
        self.assertEqual(gate.intent_label, "quote_request")

    def test_f_explicit_human_is_deterministic(self):
        fake = FakeSemanticClassifier()
        gate = decide_collection(
            current_message="Quero falar com um atendente.",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertTrue(gate.should_collect)
        self.assertEqual(gate.trigger, CollectionTrigger.HUMAN)
        self.assertEqual(fake.calls, 0)

    def test_g_explicit_budget_skips_classifier(self):
        fake = FakeSemanticClassifier()
        gate = decide_collection(
            current_message="Quero um orçamento.",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertTrue(gate.should_collect)
        self.assertEqual(fake.calls, 0)

    def test_h_classifier_unavailable_fallback(self):
        fake = FakeSemanticClassifier(
            SemanticCommercialIntentDecision(
                intent=CommercialIntentLabel.OTHER,
                commercial_intent=False,
                confidence=0.0,
                reason="",
                suggested_action=SuggestedCommercialAction.CONTINUE_CONVERSATION,
                available=False,
                error_type="Timeout",
            )
        )
        gate = decide_collection(
            current_message="Tenho três máquinas e preciso automatizá-las.",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertFalse(gate.should_collect)

    def test_i_invalid_payload_fallback(self):
        fake = FakeSemanticClassifier(
            SemanticCommercialIntentDecision(
                intent=CommercialIntentLabel.OTHER,
                commercial_intent=True,
                confidence=0.95,
                reason="x",
                suggested_action=SuggestedCommercialAction.START_COLLECTION,
                available=False,
                error_type="invalid_json",
            )
        )
        gate = decide_collection(
            current_message="Preciso automatizar minha linha.",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertFalse(gate.should_collect)

    def test_j_skips_when_commercial_intent_already_persisted(self):
        LeadDraft.objects.create(
            tenant=self.tenant,
            conversation=self.conversation,
            qualification_data={COMMERCIAL_INTENT_KEY: True, COLLECTION_ACTIVE_KEY: True},
        )
        fake = FakeSemanticClassifier()
        gate = decide_collection(
            current_message="Meu nome é João.",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertEqual(fake.calls, 0)

    def test_k_multi_turn_passes_history(self):
        fake = FakeSemanticClassifier()
        history = [
            {"role": "user", "content": "Vocês trabalham com automação?"},
            {"role": "assistant", "content": "Sim, atuamos em diversos segmentos."},
        ]
        decide_collection(
            current_message="Tenho três máquinas que preciso automatizar.",
            conversation=self.conversation,
            history=history,
            semantic_classifier=fake,
        )
        self.assertEqual(len(fake.last_kwargs.get("history") or []), 2)

    def test_l_tenant_isolation(self):
        other = Tenant.objects.create(name="Outro", slug="semantic-other")
        other_conv = Conversation.objects.create(tenant=other, session_id=str(uuid.uuid4()))
        fake = FakeSemanticClassifier()
        decide_collection(
            current_message="Tenho três máquinas e preciso automatizá-las.",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertEqual(LeadDraft.objects.filter(conversation=other_conv).count(), 0)
        self.assertEqual(fake.calls, 1)

    def test_m_study_false_positive(self):
        fake = FakeSemanticClassifier(
            _decision(
                intent=CommercialIntentLabel.TECHNICAL_QUESTION,
                commercial_intent=False,
                suggested_action=SuggestedCommercialAction.CONTINUE_CONVERSATION,
                reason="Estudo teórico.",
            )
        )
        gate = decide_collection(
            current_message="Estou estudando automação industrial e queria entender como funciona.",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertFalse(gate.should_collect)

    def test_n_institutional_installation_question(self):
        fake = FakeSemanticClassifier(
            _decision(
                intent=CommercialIntentLabel.OTHER,
                commercial_intent=False,
                suggested_action=SuggestedCommercialAction.CONTINUE_CONVERSATION,
                reason="Capacidade institucional.",
            )
        )
        gate = decide_collection(
            current_message="Vocês fazem instalação elétrica?",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertFalse(gate.should_collect)

    def test_o_real_electrical_need(self):
        fake = FakeSemanticClassifier()
        gate = decide_collection(
            current_message="Meu galpão está com problema elétrico e preciso de alguém para avaliar.",
            conversation=self.conversation,
            semantic_classifier=fake,
        )
        self.assertTrue(gate.should_collect)


class SemanticParserTests(TestCase):
    def test_parse_valid_json(self):
        payload = json.dumps(
            {
                "intent": "commercial_interest",
                "commercial_intent": True,
                "confidence": 0.82,
                "reason": "Projeto real mencionado.",
                "suggested_action": "start_collection",
            }
        )
        parsed = parse_semantic_classifier_payload(payload)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertTrue(parsed.commercial_intent)


@override_settings(**SEMANTIC_SETTINGS)
class SemanticLiviaIntegrationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Semantic Flow", slug="semantic-flow")
        self.conversation = Conversation.objects.create(tenant=self.tenant, session_id=str(uuid.uuid4()))
        self.service = LiviaDecisionService()

    def test_semantic_flow_creates_lead_and_persists_metadata(self):
        fake = FakeSemanticClassifier()
        with mock.patch(
            "assistant_core.services.semantic_commercial_intent.SemanticCommercialIntentService",
            return_value=fake,
        ):
            result = self.service.generate_reply(
                [],
                "Tenho três câmaras frigoríficas e preciso avaliar uma solução.",
                conversation=self.conversation,
                knowledge_context="",
            )
        self.assertTrue(result.reply)
        lead = LeadDraft.objects.get(conversation=self.conversation)
        self.assertTrue(lead.qualification_data.get(COLLECTION_ACTIVE_KEY))
        self.assertTrue(lead.qualification_data.get(COMMERCIAL_INTENT_KEY))
        self.assertEqual(lead.qualification_data.get("intent_source"), INTENT_SOURCE_SEMANTIC)

    def test_chat_api_survives_classifier_timeout(self):
        import json

        from django.test import Client

        client = Client()
        unavailable = SemanticCommercialIntentDecision(
            intent=CommercialIntentLabel.OTHER,
            commercial_intent=False,
            confidence=0.0,
            reason="",
            suggested_action=SuggestedCommercialAction.CONTINUE_CONVERSATION,
            available=False,
            error_type="Timeout",
        )
        payload = {
            "tenant": self.tenant.slug,
            "session_id": "semantic-timeout",
            "message": "Como funciona um CLP?",
            "request_id": str(uuid.uuid4()),
        }
        with mock.patch(
            "assistant_core.services.semantic_commercial_intent.SemanticCommercialIntentService",
        ) as mock_cls:
            inst = mock_cls.return_value
            inst.classify.return_value = unavailable
            inst.should_act_on.return_value = False
            response = client.post(
                "/api/chat/",
                data=json.dumps(payload),
                content_type="application/json",
                HTTP_X_LIVIA_REQUEST_ID=payload["request_id"],
            )
        self.assertEqual(response.status_code, 200)
