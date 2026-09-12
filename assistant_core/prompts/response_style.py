"""Shared response style for OpenAI language generation."""

import re

from assistant_core.conversation_turns import normalize_text


def requests_explicit_detail(message: str) -> bool:
    normalized = normalize_text(message)
    for clause in re.split(r"[.!?;]+", normalized):
        if re.search(r"\b(nao|sem|dispenso)\b", clause):
            continue
        if re.search(
            r"\b(?:(?:explicar|explique|explica) "
            r"(?:melhor|com mais detalhes|detalhadamente|passo a passo)"
            r"|(?:pode|poderia) (?:detalhar|aprofundar)"
            r"|(?:detalhe|aprofunde)"
            r"|(?:me de|quero) mais detalhes"
            r"|quero entender melhor)\b",
            clause,
        ):
            return True
    return False


def build_response_style(message: str) -> str:
    mode = (
        "- Neste turno há pedido explícito de aprofundamento: desenvolva a explicação "
        "com mais contexto relevante, sempre limitado às evidências disponíveis."
        if requests_explicit_detail(message)
        else "- Neste turno prefira uma resposta curta; uma pergunta com 'como' por si só "
        "não exige explicação extensa."
    )
    return "\n".join([
        "ESTILO E EXTENSÃO:",
        "- Responda apenas o necessário para resolver o turno atual, de forma direta, objetiva e natural.",
        "- Para perguntas comuns, prefira aproximadamente 2 a 4 frases curtas; isso é uma orientação, não um limite rígido.",
        "- Comece pela informação que resolve a necessidade imediata do usuário.",
        "- Não transforme respostas simples em artigos ou manuais.",
        "- Não repita a pergunta do usuário nem repita conclusões.",
        "- Não despeje trechos da base de conhecimento; sintetize somente as evidências necessárias para responder.",
        "- Faça no máximo uma pergunta de continuidade quando necessária; não pergunte apenas para manter a conversa.",
        "- No diagnóstico, peça uma informação por vez quando possível, sem acumular perguntas extras.",
        "- Se o usuário pedir explicitamente mais detalhes, aprofunde a explicação sem repetição e mantendo grounding.",
        "- Se segurança ou precisão técnica exigirem contexto adicional, forneça o necessário mesmo sem pedido de detalhes.",
        "- Não omita condicionantes ou limitações da evidência para encurtar a resposta.",
        mode,
    ])
