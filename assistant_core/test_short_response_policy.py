"""Offline contracts for concise OpenAI responses and optional follow-ups."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from assistant_core.dialogue_memory import DialogueMemory
from assistant_core.followup_strategy import select_followup
from assistant_core.prompts.livia_ai import build_livia_ai_prompt
from assistant_core.prompts.openai_conversation import build_openai_conversation_prompt
from assistant_core.prompts.response_style import requests_explicit_detail
from assistant_core.services.openai_grounded_conversation import OpenAIGroundedConversationService
from assistant_core.services.response_quality_gate import apply_response_quality_gate
from integrations.openai.client import OpenAIChatResult


class ShortResponsePolicyTests(SimpleTestCase):
    def prompt(self, message, **kwargs):
        return build_openai_conversation_prompt(
            tenant=SimpleNamespace(name="Tenant A", slug="tenant-a"),
            assistant_profile=None, message=message, conversation=None,
            discovery_result=None, knowledge_context="", **kwargs,
        )

    def test_explicit_detail_examples(self):
        for message in (
            "pode explicar melhor", "explique melhor", "pode detalhar",
            "me dê mais detalhes", "quero mais detalhes", "explique com mais detalhes",
            "quero entender melhor", "pode aprofundar", "explique passo a passo",
            "Pode explicar detalhadamente como funciona o Duno?",
            "PODE DETALHAR?", "  Me dê   mais detalhes!",
        ):
            with self.subTest(message=message):
                self.assertTrue(requests_explicit_detail(message))

    def test_common_questions_and_negations_do_not_request_detail(self):
        for message in (
            "Como funciona o Duno?", "O Duno faz limpeza sozinho?", "",
            "Meu ar-condicionado está mostrando E5.", "Não quero mais detalhes.",
            "Pode explicar sem mais detalhes?", "Como peço orçamento?",
        ):
            with self.subTest(message=message):
                self.assertFalse(requests_explicit_detail(message))

    def test_simple_question_requests_synthesis_not_catalog(self):
        system = self.prompt("O Duno faz limpeza sozinho?")[0]["content"]
        for rule in (
            "aproximadamente 2 a 4 frases curtas", "Neste turno prefira uma resposta curta",
            "Não transforme respostas simples em artigos ou manuais",
            "sintetize somente as evidências necessárias", "Não repita a pergunta",
            "Comece pela informação", "no máximo uma pergunta",
        ):
            self.assertIn(rule, system)

    def test_detail_mode_does_not_carry_over_from_history(self):
        detailed = self.prompt("Pode explicar melhor?")[0]["content"]
        self.assertIn("Neste turno há pedido explícito de aprofundamento", detailed)
        brief = self.prompt("Obrigado", history=[
            {"role": "user", "content": "Pode explicar melhor?"},
        ])[0]["content"]
        self.assertIn("Neste turno prefira uma resposta curta", brief)

    def test_safety_and_grounding_exceptions_exist_in_both_modes(self):
        for message in ("Como funciona?", "Pode detalhar?"):
            system = self.prompt(message)[0]["content"]
            for rule in (
                "segurança ou precisão técnica", "mesmo sem pedido de detalhes",
                "Não omita condicionantes", "Nunca invente fatos técnicos",
                "única fonte factual empresarial", "Não complete lacunas",
                "só afirme o que a evidência confirmar diretamente",
                "Quando não houver evidência suficiente", "collection_active=false: NÃO peça",
            ):
                self.assertIn(rule, system)

    def test_commercial_fields_remain_authoritative(self):
        prompt = self.prompt("Pode detalhar?", commercial_state={
            "collection_active": True, "allowed_collection_fields": ["phone_or_email"],
            "known_lead_fields": {"name": "Ana"},
        })
        self.assertIn("collection_active: True", prompt[0]["content"])
        self.assertIn("campos comerciais permitidos: phone_or_email", prompt[1]["content"])
        self.assertIn("name=Ana", prompt[1]["content"])
        self.assertIn("Você NÃO altera lead_state", prompt[0]["content"])

    def test_legacy_rewriter_uses_same_style_without_changing_operational_rules(self):
        prompt = build_livia_ai_prompt(
            tenant=None, assistant_profile=None, message="Pode detalhar?", conversation=None,
            discovery_result=None, lead_state="discovery", knowledge_context="",
            deterministic_reply="Resposta base.",
        )[0]["content"]
        self.assertIn("ESTILO E EXTENSÃO", prompt)
        self.assertIn("Neste turno há pedido explícito", prompt)
        self.assertIn("Não mude decisões operacionais", prompt)

    @patch("assistant_core.services.openai_grounded_conversation.record_ai_usage")
    def test_mocked_generation_preserves_full_grounded_detail(self, telemetry):
        answer = (
            "O Duno é voltado à limpeza autônoma. A documentação descreve varredura. "
            "Também descreve lavagem. A escolha depende do piso. "
            "O fluxo de pessoas precisa ser avaliado. Os obstáculos também precisam ser avaliados."
        )
        client = Mock()
        client.create_chat_completion.return_value = OpenAIChatResult(
            text=answer, success=True, dry_run=False,
        )
        result = OpenAIGroundedConversationService(ai_client=client).generate(
            tenant=None, assistant_profile=None, message="Pode explicar melhor?",
            conversation=None, discovery=None, decision=None, knowledge_context=answer,
        )
        self.assertTrue(result.used)
        self.assertEqual(result.text, answer)
        system = client.create_chat_completion.call_args.kwargs["messages"][0]["content"]
        self.assertIn("Neste turno há pedido explícito", system)
        final, _ = apply_response_quality_gate(
            reply=result.text, knowledge_context=answer,
            current_message="Pode explicar melhor?", llm_primary=True,
        )
        self.assertEqual(final, answer)

    def test_missing_evidence_keeps_honest_diagnostic_response(self):
        answer = "Não encontrei informação suficiente na minha base para confirmar isso. Qual é a marca?"
        final, _ = apply_response_quality_gate(
            reply=answer, knowledge_context="", current_message="Meu ar-condicionado está mostrando E5.",
            llm_primary=True,
        )
        self.assertEqual(final, answer)
        self.assertEqual(final.count("?"), 1)

    def test_existing_question_prevents_extra_followup_even_when_forced(self):
        for force in (False, True):
            for answer in ("Qual é a marca?", "Qual é a marca? Ainda preciso confirmar o código."):
                follow, _ = select_followup(
                    memory=DialogueMemory(active_domain="automation"),
                    current_message="Meu equipamento mostra E5", answer_text=answer, force=force,
                )
                self.assertEqual(follow, "")

    def test_needed_diagnostic_followup_still_works(self):
        follow, _ = select_followup(
            memory=DialogueMemory(active_domain="automation"),
            current_message="Preciso de automação", answer_text="Posso te orientar.",
        )
        self.assertEqual(follow, "Qual equipamento ou processo você precisa automatizar?")

    def test_direct_question_does_not_get_conversation_filler(self):
        follow, _ = select_followup(
            memory=DialogueMemory(active_domain="robotics"),
            current_message="Como funciona o Duno?",
            answer_text="O Duno é voltado à limpeza autônoma.",
        )
        self.assertEqual(follow, "")
