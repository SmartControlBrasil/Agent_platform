from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from assistant_core.prompts.livia import DEFAULT_REPLY
from assistant_core.state import LeadState

NAME_DEFERRED_PATTERNS = (
    r"\bnao quero (falar|informar|passar|dar)\b.{0,40}\bnome\b",
    r"\bnao quero falar meu nome\b",
    r"\bprefiro nao informar\b",
    r"\bprefiro nao (falar|passar|dar) (meu )?nome\b",
    r"\bdepois eu passo (o |meu )?nome\b",
    r"\bpodemos continuar sem (o |meu )?nome\b",
    r"\bsem (o |meu )?nome por agora\b",
    r"\bainda nao quero (dar|falar|informar) (o |meu )?nome\b",
    r"\bagora nao( quero)? (falar|informar|passar) (o |meu )?nome\b",
)

ENRICHMENT_PHRASES = (
    "trabalho com",
    "trabalhamos com",
    "minha loja",
    "meu negocio",
    "nossa loja",
    "produtos para",
    "produto para",
    "clientes de",
)
ENRICHMENT_WORDS = (
    "vendo",
    "atendo",
    "atendemos",
    "comercializo",
    "produzo",
    "fabrico",
    "catalogo",
    "segmento",
)

ENRICHMENT_CAPTURE_PATTERNS = (
    r"(?:vendo|comercializo|fabrico|produzo)\s+(.+)$",
    r"(?:trabalho com|trabalhamos com|atendo|atendemos)\s+(.+)$",
    r"(?:produtos para|produto para|loja (?:de|para)|catalogo de|catálogo de)\s+(.+)$",
    r"(?:minha loja vende|nossa loja vende|a loja vende)\s+(.+)$",
)

QUESTION_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeline", ("quanto tempo", "qual o prazo", "qual prazo", "prazo para", "demora", "demora para", "leva quanto")),
    ("price", ("quanto custa", "qual o preco", "qual o preço", "qual valor", "preco", "preço")),
    ("payment", ("pagamento", "pagar online", "checkout", "meio de pagamento", "pagamento online")),
    ("shipping", ("frete", "calculo de frete", "cálculo de frete", "calcular frete")),
    ("mobile", ("celular", "smartphone", "mobile", "responsiv", "funciona no celular")),
    ("catalog", ("cadastrar produto", "cadastrar meus produto", "catalogo", "catálogo")),
    ("maintenance", ("manutencao depois", "manutenção depois", "fazem manutencao", "fazem manutenção")),
    ("how_it_works", ("como funciona", "como e o processo", "como é o processo")),
)

STOPWORDS = {
    "a", "ao", "as", "com", "da", "das", "de", "do", "dos", "e", "em", "eu", "me",
    "o", "os", "para", "por", "que", "um", "uma", "uns", "umas",
}

ENVIRONMENT_MARKERS = (
    "galpao",
    "galpão",
    "condominio",
    "condomínio",
    "armazem",
    "armazém",
    "deposito",
    "depósito",
    "shopping",
    "supermercado",
    "hospital",
    "industria",
    "indústria",
    "fabrica",
    "fábrica",
    "concreto",
    "porcelanato",
    "epoxi",
    "epóxi",
    "ceramica",
    "cerâmica",
    "piso",
    "metragem",
    "metro quadrado",
    "m2",
    "m²",
)


def looks_like_environment_answer(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(marker in normalized for marker in ENVIRONMENT_MARKERS):
        return True
    return bool(re.search(r"\b\d+\s*m\b", normalized))


def looks_like_business_segment_description(text: str) -> bool:
    """Ramo/segmento consultivo (ex.: loja de bebidas) — não é razão social."""
    normalized = normalize_text(text)
    if not normalized:
        return False
    if re.search(r"^(?:e|é|eh)\s+(?:uma|um)\s+", normalized):
        return True
    if re.search(r"^(?:somos|sou)\s+(?:uma|um)\s+", normalized):
        return True
    if re.search(r"\bloja de\b", normalized) and len(normalized.split()) <= 8:
        return True
    if re.search(r"\b(?:negocio|negócio|ramo|segmento) de\b", normalized):
        return True
    if re.search(r"^(?:minha|nossa)\s+loja\b", normalized) and " ltda" not in normalized:
        return True
    return False


def is_consultative_context_answer(text: str) -> bool:
    """Resposta técnica/contextual — não preenche slot comercial de nome/contato."""
    normalized = normalize_text(text)
    if not normalized:
        return False
    if looks_like_business_segment_description(text):
        return True
    if looks_like_environment_answer(normalized):
        return True
    if is_direct_question(text):
        return True
    from assistant_core.services.deterministic_synthesis import is_short_context_token

    if is_short_context_token(normalized):
        return True
    content_words = [word for word in normalized.split() if word not in STOPWORDS and len(word) > 2]
    if 1 <= len(content_words) <= 2 and looks_like_environment_answer(normalized):
        return True
    return False

NAME_DEFERRED_KEY = "name_deferred"
CONTACT_DEFERRED_KEY = "contact_collection_deferred"


def normalize_text(text: str) -> str:
    normalized = str(text or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", normalized)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", normalized)


class TurnKind(str, Enum):
    NAME_DEFERRED = "name_deferred"
    DIRECT_QUESTION = "direct_question"
    NEED_ENRICHMENT = "need_enrichment"
    OTHER = "other"


@dataclass(frozen=True)
class ConversationTurn:
    kind: TurnKind
    question_type: str = ""
    enrichment_snippet: str = ""
    continue_commercial_thread: bool = False


def is_name_deferred(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(re.search(pattern, normalized) for pattern in NAME_DEFERRED_PATTERNS)


def is_contact_deferred_message(text: str) -> bool:
    from assistant_core.dialogue_memory import is_contact_deferred

    return is_contact_deferred(text)


def is_direct_question(text: str) -> bool:
    if detect_question_type(text):
        return True
    normalized = normalize_text(text)
    if "?" not in str(text or ""):
        return False
    followup_markers = (
        "ele ", "ela ", "esse ", "essa ", "este ", "esta ",
        "consegue", "funciona", "trabalha", "limpa", "suporta",
        "cabe", "da para", "dá para", "e possivel", "é possível",
        "autonomia", "bateria", "circulando", "noite",
    )
    return any(marker in normalized for marker in followup_markers)


def detect_question_type(text: str) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return ""
    looks_like_question = (
        "?" in str(text or "")
        or normalized.startswith(("quanto ", "qual ", "quais ", "como ", "tem ", "voces ", "voce ", "da para", "da pra", "e possivel", "possivel"))
    )
    if not looks_like_question:
        return ""
    for question_type, markers in QUESTION_TYPES:
        if any(marker in normalized for marker in markers):
            return question_type
    return ""


def is_need_enrichment(text: str) -> bool:
    from assistant_core.consultative_policy import is_explicit_human_handoff

    if is_explicit_human_handoff(text):
        return False
    if is_name_deferred(text) or is_direct_question(text):
        return False
    normalized = normalize_text(text)
    if not normalized:
        return False
    if looks_like_environment_answer(normalized):
        return True
    if re.search(r"\b(?:meu nome|telefone|whatsapp|email|e-mail)\b", normalized):
        return False
    from assistant_core.services.deterministic_synthesis import is_short_context_token

    if is_short_context_token(normalized):
        return True
    if any(phrase in normalized for phrase in ENRICHMENT_PHRASES):
        return True
    if any(re.search(rf"\b{re.escape(word)}\b", normalized) for word in ENRICHMENT_WORDS):
        return True
    content_words = [word for word in normalized.split() if word not in STOPWORDS and len(word) > 2]
    if len(content_words) < 3:
        return False
    # Evita tratar rótulos de nome/empresa como enrichment (ex.: "Marcelo Teste RAG").
    if _looks_like_name_or_company_label(normalized, content_words):
        return False
    return True


def _looks_like_name_or_company_label(normalized: str, content_words: list[str]) -> bool:
    if len(content_words) > 5:
        return False
    need_markers = (
        "preciso", "quero", "gostaria", "loja", "site", "sistema", "produto", "produtos",
        "orcamento", "orçamento", "automacao", "automação", "robo", "robô", "pia", "cozinha",
        "para", "com", "sobre", "venda", "vendo", "atendo",
    )
    if any(marker in normalized for marker in need_markers):
        return False
    return True


def extract_enrichment_snippet(text: str) -> str:
    raw = " ".join(str(text or "").strip().split())
    normalized = normalize_text(raw)
    for pattern in ENRICHMENT_CAPTURE_PATTERNS:
        match = re.search(pattern, normalized)
        if match:
            snippet = match.group(1).strip(" .;,-")
            snippet = re.split(r"\b(?:e preciso|quero|gostaria)\b", snippet)[0].strip()
            if 2 <= len(snippet) <= 80:
                return snippet
    return ""


def merge_need_summaries(existing: str, incoming: str) -> str:
    current = " ".join(str(existing or "").strip().split())
    addition = " ".join(str(incoming or "").strip().split())
    if not addition:
        return current[:500]
    if not current:
        return addition[:500]
    if normalize_text(addition) in normalize_text(current):
        return current[:500]
    merged = f"{current.rstrip('. ')}. {addition}"
    return merged[:500]


def _lead_model_has_qualification_data(lead_draft) -> bool:
    meta = getattr(getattr(lead_draft, "__class__", None), "_meta", None)
    if meta is None:
        return False
    try:
        meta.get_field("qualification_data")
        return True
    except Exception:
        return False


def lead_has_name_deferred(lead_draft) -> bool:
    if lead_draft is None:
        return False
    if _lead_model_has_qualification_data(lead_draft):
        data = getattr(lead_draft, "qualification_data", None) or {}
        if isinstance(data, dict) and data.get(NAME_DEFERRED_KEY):
            return True
    conversation = getattr(lead_draft, "conversation", None)
    if conversation is None:
        return False
    try:
        messages = conversation.messages.all()
    except Exception:
        return False
    for message in messages:
        role = str(getattr(message, "role", "") or "")
        content = str(getattr(message, "content", "") or "")
        if role == "user" and is_name_deferred(content):
            return True
    return False


def mark_name_deferred(lead_draft) -> None:
    if lead_draft is None or not _lead_model_has_qualification_data(lead_draft):
        return
    data = dict(getattr(lead_draft, "qualification_data", None) or {})
    if data.get(NAME_DEFERRED_KEY) is True:
        return
    data[NAME_DEFERRED_KEY] = True
    lead_draft.qualification_data = data
    update_fields = ["qualification_data"]
    if hasattr(lead_draft, "updated_at"):
        update_fields.append("updated_at")
    lead_draft.save(update_fields=update_fields)


def has_prior_commercial_thread(conversation, history) -> bool:
    lead_state = str(getattr(conversation, "lead_state", "") or "")
    if lead_state in {LeadState.COLLECT_NEED, LeadState.COLLECT_NAME_COMPANY, LeadState.COLLECT_CONTACT, LeadState.OFFER_HANDOFF}:
        return True
    lead = _conversation_lead(conversation)
    if lead is not None and (str(getattr(lead, "need_summary", "") or "").strip() or lead_has_name_deferred(lead)):
        return True
    for item in history or []:
        if str(item.get("role") or "") != "user":
            continue
        content = normalize_text(item.get("content") or "")
        if any(marker in content for marker in ("preciso", "quero", "gostaria", "orcamento", "orçamento", "loja", "site", "sistema", "automacao", "automação")):
            return True
    return False


def has_open_commercial_thread(conversation, history, discovery) -> bool:
    if has_prior_commercial_thread(conversation, history):
        return True
    if discovery is not None and (getattr(discovery, "has_commercial_interest", False) or getattr(discovery, "has_quote_request", False)):
        if getattr(discovery, "intent", "") != "greeting":
            return True
    return False


def classify_conversation_turn(*, current_message: str, history, conversation, discovery) -> ConversationTurn:
    from assistant_core.consultative_policy import is_explicit_collection_trigger
    from assistant_core.dialogue_memory import is_contact_deferred, wants_consultative_continue
    from assistant_core.services.decision_outcome import (
        _is_product_information_discovery,
        is_consultative_knowledge_message,
    )

    thread = has_prior_commercial_thread(conversation, history)
    if is_explicit_collection_trigger(current_message):
        # Explicit budget/hire/human handoff must reach the qualification path.
        return ConversationTurn(kind=TurnKind.OTHER, continue_commercial_thread=True)
    from assistant_core.services.decision_outcome import is_consultative_knowledge_turn

    if is_consultative_knowledge_message(current_message):
        return ConversationTurn(kind=TurnKind.OTHER, continue_commercial_thread=thread)
    if is_name_deferred(current_message) or is_contact_deferred(current_message) or wants_consultative_continue(current_message):
        return ConversationTurn(kind=TurnKind.NAME_DEFERRED, continue_commercial_thread=True)
    from assistant_core.consultative_policy import collection_already_active, _is_direct_need_slot_answer
    from assistant_core.qualification.livia import message_fills_pending_slot
    from leads.services.commercial import QualificationService

    active_lead = _conversation_lead(conversation)
    if collection_already_active(conversation, active_lead):
        pending = QualificationService().missing_fields(active_lead) if active_lead is not None else []
        if pending:
            if pending[0] == "need_summary" and active_lead is not None and _is_direct_need_slot_answer(current_message, active_lead):
                return ConversationTurn(kind=TurnKind.OTHER, continue_commercial_thread=True)
            if pending[0] != "need_summary" and message_fills_pending_slot(current_message, pending[0]):
                return ConversationTurn(kind=TurnKind.OTHER, continue_commercial_thread=True)
    question_type = detect_question_type(current_message)
    if question_type or is_direct_question(current_message):
        return ConversationTurn(
            kind=TurnKind.DIRECT_QUESTION,
            question_type=question_type,
            continue_commercial_thread=True,
        )
    if not is_explicit_collection_trigger(current_message) and not bool(getattr(discovery, "should_collect_lead", False)):
        if _is_product_information_discovery(discovery):
            return ConversationTurn(kind=TurnKind.OTHER, continue_commercial_thread=thread)
    if is_consultative_knowledge_turn(discovery, current_message):
        return ConversationTurn(kind=TurnKind.OTHER, continue_commercial_thread=thread)
    if thread and (is_need_enrichment(current_message) or is_consultative_context_answer(current_message)):
        return ConversationTurn(
            kind=TurnKind.NEED_ENRICHMENT,
            enrichment_snippet=extract_enrichment_snippet(current_message),
            continue_commercial_thread=True,
        )
    return ConversationTurn(kind=TurnKind.OTHER, continue_commercial_thread=thread)


def build_name_deferred_reply(lead_draft=None) -> str:
    from assistant_core.consultative_policy import pause_collection
    from assistant_core.dialogue_memory import is_contact_deferred, wants_consultative_continue

    if lead_draft is not None:
        pause_collection(lead_draft, deferred_contact=True)
    need = str(getattr(lead_draft, "need_summary", "") or "").strip()
    # Recusa de contato / "tire minhas dúvidas": não força próxima pergunta comercial.
    return (
        "Tudo bem — seguimos só com as dúvidas por enquanto. "
        "Pode me perguntar o que quiser sobre a solução."
    )


def build_enrichment_reply(
    lead_draft=None,
    *,
    snippet: str = "",
    current_message: str = "",
    history=None,
    followup: str = "",
) -> str:
    need = str(getattr(lead_draft, "need_summary", "") or "").strip() or str(current_message or "").strip()
    detail = snippet or extract_enrichment_snippet(current_message)
    next_question = followup or _catalog_or_scope_question(
        need,
        history=history,
        current_message=current_message,
    )
    if detail and need:
        body = f"Entendi. Anotei que isso envolve {detail}, no contexto de {_short_need(need)}."
    elif looks_like_environment_answer(current_message):
        body = "Entendi, isso ajuda a detalhar a necessidade."
    elif detail:
        body = f"Entendi, você trabalha com {detail}."
    else:
        body = "Entendi, isso ajuda a detalhar a necessidade."
    if next_question:
        return f"{body} {next_question}".strip()
    return body


def build_direct_question_reply(lead_draft=None, *, question_type: str, current_message: str = "") -> str:
    if question_type == "price":
        from assistant_core.consultative_policy import build_conceptual_price_reply

        return build_conceptual_price_reply(lead_draft, current_message=current_message)
    need = str(getattr(lead_draft, "need_summary", "") or "").strip()
    answer = _grounded_question_answer(question_type, need)
    follow_up = _follow_up_after_question(question_type, need)
    return f"{answer} {follow_up}".strip()


def skip_name_prompt_fields(missing_fields: list[str], lead_draft, *, current_message: str = "") -> list[str]:
    fields = list(missing_fields or [])
    data = dict(getattr(lead_draft, "qualification_data", None) or {}) if lead_draft is not None else {}
    if "name_or_company" in fields:
        if lead_has_name_deferred(lead_draft) or is_name_deferred(current_message) or is_direct_question(current_message) or is_need_enrichment(current_message):
            fields = [field for field in fields if field != "name_or_company"]
    # Recusa de contato: não pede telefone/e-mail, mas ainda pode pedir nome se estiver ativo.
    if data.get(CONTACT_DEFERRED_KEY) or is_contact_deferred_message(current_message):
        fields = [field for field in fields if field not in {"phone", "email", "contact", "phone_or_email"}]
    return fields


def _conversation_lead(conversation):
    if conversation is None:
        return None
    from leads.services.commercial import resolve_lead_draft

    return resolve_lead_draft(conversation)


def _short_need(need: str) -> str:
    cleaned = " ".join(str(need or "").strip().split())
    if not cleaned:
        return "sua necessidade"
    if len(cleaned) <= 80:
        return cleaned.rstrip(".")
    return cleaned[:77].rstrip() + "…"


def _catalog_or_scope_question(need: str, *, history=None, current_message: str = "") -> str:
    from assistant_core.consultative_slots import (
        extract_consultative_slots,
        is_cleaning_consultation,
        select_cleaning_followup,
        should_skip_followup_for_answered_slots,
    )
    from assistant_core.dialogue_memory import infer_application, infer_domain, infer_topic

    normalized = normalize_text(need)
    # Robótica/limpeza/produto SCB antes de heurística genérica de "produto/site".
    if any(marker in normalized for marker in ("duno", "dune", "hygibot", "limpeza", "lavar", "varrer", "aspirar")):
        class _Memory:
            active_application = infer_application(need)
            active_topic = infer_topic(need)
            active_domain = infer_domain(need)

        if is_cleaning_consultation(memory=_Memory(), need_summary=need, current_message=current_message):
            slots = extract_consultative_slots(
                need_summary=need,
                history=history,
                current_message=current_message,
            )
            followup = select_cleaning_followup(slots=slots, current_message=current_message)
            if should_skip_followup_for_answered_slots(
                followup,
                need_summary=need,
                history=history,
                current_message=current_message,
            ):
                return ""
            return followup
        return "Qual é o ambiente e o tipo de piso onde a limpeza acontece?"
    if any(marker in normalized for marker in ("automacao", "automação", "robo", "robô", "robotica", "robótica", "xyron", "mitsubishi", "clp")):
        return "Qual ambiente e objetivo você quer cobrir primeiro?"
    if any(marker in normalized for marker in ("escola", "educac", "professor", "bncc")) and "limpeza" not in normalized:
        return "O foco é robótica educacional, demonstração ou outro objetivo na escola?"
    if any(marker in normalized for marker in ("cozinha", "bancada", "pia", "cooktop", "ilha", "banheiro", "lavabo", "escada", "gourmet", "granito", "marmore", "mármore")):
        return "Você já tem medidas aproximadas, fotos ou algum material em mente?"
    if any(marker in normalized for marker in ("loja virtual", "ecommerce", "e-commerce", "loja online")):
        return "Você pretende começar com poucos produtos ou já tem um catálogo maior?"
    if any(marker in normalized for marker in ("sistema", "portal", "dashboard", "aplicativo", "app")):
        return "Quais processos esse sistema precisa cobrir primeiro?"
    if any(marker in normalized for marker in ("site institucional", "landing page", "pagina web", "página web")):
        return "O foco é divulgação, captura de contatos ou outro objetivo do site?"
    return "Qual detalhe é mais importante para você neste momento?"


def _next_discovery_question(need: str, snippet: str) -> str:
    if snippet:
        return _catalog_or_scope_question(f"{need} {snippet}")
    return _catalog_or_scope_question(need)


def _grounded_question_answer(question_type: str, need: str) -> str:
    normalized_need = normalize_text(need)
    stone_context = any(
        marker in normalized_need
        for marker in (
            "cozinha", "bancada", "pia", "cooktop", "banheiro", "lavabo", "escada",
            "gourmet", "granito", "marmore", "mármore", "nicho", "cuba", "churrasqueira",
        )
    )
    if question_type == "timeline":
        if stone_context:
            return (
                "O prazo depende do tipo de projeto, medidas, material escolhido e da complexidade "
                "da execução. Posso levantar esses pontos com você para a equipe orientar melhor."
            )
        return (
            "O prazo depende principalmente da quantidade de produtos, meios de pagamento, "
            "frete, integrações e conteúdo. Posso levantar esses pontos com você para chegar a uma estimativa mais precisa."
        )
    if question_type == "price":
        if stone_context:
            return (
                "O investimento varia conforme material, medidas, acabamentos e complexidade do projeto. "
                "Ainda não tenho um valor fechado para informar aqui."
            )
        return "O investimento varia conforme o escopo, volume e integrações envolvidas. Ainda não tenho um valor fechado para informar aqui."
    if question_type == "payment":
        return "Pagamento online pode entrar no projeto, mas o detalhe depende do checkout e dos meios que você precisa usar."
    if question_type == "shipping":
        return "Cálculo de frete também pode fazer parte do projeto, conforme a operação logística que você já usa ou pretende usar."
    if question_type == "mobile":
        return "O acesso em celular costuma fazer parte desse tipo de projeto, e o desenho final depende de como as pessoas vão usar."
    if question_type == "catalog":
        return "Sim, o cadastro de produtos é um ponto central. O esforço muda bastante se o catálogo começa pequeno ou já chega grande."
    if question_type == "maintenance":
        if stone_context:
            return "Os cuidados depois da instalação dependem do material. Posso te orientar com as recomendações gerais e a equipe detalha no atendimento."
        return "A manutenção depois da entrega depende do que for combinado no projeto. Posso registrar essa necessidade para a equipe detalhar."
    if question_type == "how_it_works":
        if stone_context:
            return (
                "O caminho usual é entender o ambiente e o projeto, receber fotos ou medidas aproximadas "
                "quando possível e só então avançar para o orçamento com a equipe."
            )
        return "O caminho usual é entender a necessidade, organizar o escopo e só então estimar prazo e próximas etapas."
    return "Posso te orientar com o que já temos da necessidade, sem fechar condição comercial daqui."


def _follow_up_after_question(question_type: str, need: str) -> str:
    if question_type in {"timeline", "price", "catalog", "payment", "shipping"}:
        return _catalog_or_scope_question(need)
    return "O que é mais urgente para você neste momento?"


def is_generic_fallback_reply(reply: str) -> bool:
    from assistant_core.services.deterministic_synthesis import is_generic_fallback_reply as _impl

    return _impl(reply)
