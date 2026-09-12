from __future__ import annotations

LIVIA_SYSTEM_PROMPT = """
Você é a Lívia, assistente virtual da Lívia Platform.

Você deve responder em português do Brasil, com tom cordial, objetivo e profissional.
Suas respostas iniciais devem ser curtas e úteis.

Objetivos da conversa:
- entender a necessidade do visitante
- responder com valor prático
- identificar quando o usuário quer orçamento, suporte técnico ou contato comercial
- capturar dados básicos de contato sem parecer formulário

Regras:
- faça no máximo uma pergunta por resposta
- nunca invente preços
- não chame OpenAI nesta etapa
- se houver dúvida técnica, responda de forma transparente
- se houver interesse comercial, siga com uma próxima pergunta simples
""".strip()

GREETING_REPLY = "Olá! Sou a Lívia. Como posso te ajudar?"

TECHNICAL_REPLY = (
    "Posso te dar uma pré-análise. Me conta qual é o cenário, o erro ou o comportamento que você está vendo."
)

DEFAULT_REPLY = "Entendi. Pode me explicar um pouco mais para eu te orientar da melhor forma?"


def budget_started_reply() -> str:
    return (
        "Claro. Vou te ajudar com isso de forma objetiva. "
        "Me conta rapidamente qual é a sua necessidade principal para eu te orientar melhor."
    )


def commercial_started_reply() -> str:
    return (
        "Entendi. Vamos organizar isso com calma. "
        "Me conta rapidamente o que você precisa para eu seguir no ponto certo."
    )


def contact_started_reply() -> str:
    return (
        "Perfeito. Vou só completar as informações essenciais para seguir com o atendimento."
    )


def ask_need_summary_reply() -> str:
    return "Perfeito. Em uma frase, me conta qual é a sua necessidade principal."


def ask_name_or_company_reply() -> str:
    return "Ótimo. Para eu dar sequência, qual é o seu nome ou o nome da empresa?"


def ask_phone_or_email_reply() -> str:
    return "Entendi. Me passa seu telefone/WhatsApp ou e-mail para eu continuar."


def qualified_reply() -> str:
    return "Perfeito. Registrei seus dados para continuidade do atendimento."


def build_contextual_reply(*, intent: str, missing_fields: list[str] | None = None) -> str:
    if missing_fields is None:
        if intent == "quote_request":
            return budget_started_reply()
        if intent == "commercial_interest":
            return commercial_started_reply()
        if intent == "contact_data":
            return contact_started_reply()
        missing_fields = []
    missing_fields = list(missing_fields or [])

    if intent == "greeting":
        return GREETING_REPLY
    if intent == "technical_question":
        return TECHNICAL_REPLY
    if intent == "support_request":
        return (
            "Posso te ajudar a entender o caso. Me conta o que está acontecendo e qual comportamento você esperava."
        )
    if intent == "quote_request":
        if not missing_fields:
            return qualified_reply()
        return _reply_for_missing_fields(missing_fields)
    if intent == "commercial_interest":
        if not missing_fields:
            return qualified_reply()
        return _reply_for_missing_fields(missing_fields)
    if intent == "contact_data":
        if not missing_fields:
            return qualified_reply()
        return _reply_for_missing_fields(missing_fields)
    return DEFAULT_REPLY


def _reply_for_missing_fields(missing_fields: list[str]) -> str:
    first_missing = missing_fields[0]
    if first_missing == "need_summary":
        return ask_need_summary_reply()
    granular_prompts = {
        "name": "Qual é o seu nome?",
        "phone": "Qual é o melhor telefone ou WhatsApp para contato?",
        "email": "Qual é o seu e-mail?",
        "company": "Qual empresa você representa, se for o caso?",
    }
    if first_missing in granular_prompts:
        return granular_prompts[first_missing]
    if first_missing == "name_or_company":
        return ask_name_or_company_reply()
    if first_missing == "phone_or_email":
        return ask_phone_or_email_reply()
    return DEFAULT_REPLY
