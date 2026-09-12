"""Memória determinística de domínio/entidade para diálogo consultivo multi-turno."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from assistant_core.conversation_turns import normalize_text

ACTIVE_ENTITY_KEY = "active_entity"
ACTIVE_DOMAIN_KEY = "active_domain"
ACTIVE_TOPIC_KEY = "active_topic"
ACTIVE_APPLICATION_KEY = "active_application"
ACTIVE_KNOWLEDGE_SUBJECT_KEY = "active_knowledge_subject"
ACTIVE_NEED_KEY = "active_need"
CONTACT_DEFERRED_KEY = "contact_collection_deferred"
COMMERCIAL_INTENT_KEY = "commercial_intent"
COLLECTION_PAUSED_KEY = "collection_paused"

# Aliases derivados de corpus/documentação conhecida (não hardcode de state machine).
# Equivalências só quando documentação confirma (HygiBot/Dune; "Duno" = typo comum de Dune).
ENTITY_REGISTRY: tuple[dict, ...] = (
    {
        "canonical": "Duno",
        "aliases": ("duno", "dune", "hygibot dune", "hygibot duno", "hygibot", "dune bot", "duno bot"),
        "domain": "robotics",
        "topic": "cleaning_robot",
        "keywords": ("limpeza", "lavar", "varrer", "aspirar", "passar pano"),
    },
    {
        "canonical": "LIRO",
        "aliases": ("liro", "littlebot", "little bot"),
        "domain": "robotics",
        "topic": "educational_robot",
        "keywords": ("educacional", "crianca", "criança", "escola", "bncc", "robotica educacional"),
    },
    {
        "canonical": "NeoBot",
        "aliases": ("neobot", "neo bot"),
        "domain": "robotics",
        "topic": "service_robot",
        "keywords": ("recepcao", "recepção", "atendimento"),
    },
    {
        "canonical": "Orbit",
        "aliases": ("orbit",),
        "domain": "robotics",
        "topic": "service_robot",
        "keywords": (),
    },
    {
        "canonical": "Buddy",
        "aliases": ("buddy",),
        "domain": "robotics",
        "topic": "service_robot",
        "keywords": (),
    },
    {
        "canonical": "Mitsubishi",
        "aliases": ("mitsubishi", "clp mitsubishi", "ihm mitsubishi"),
        "domain": "automation",
        "topic": "industrial_automation",
        "keywords": ("clp", "ihm", "automacao", "automação"),
    },
)

DOMAIN_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("software_web", ("python", "sistema web", "sistemas web", "loja virtual", "ecommerce", "e-commerce", "website", "portal", "django", "site")),
    ("automation", ("automacao", "automação", "mitsubishi", "clp", "ihm")),
    ("maintenance", ("manutencao tecnica", "manutenção técnica", "tpm", "pecas", "peças", "suporte tecnico", "suporte técnico")),
    ("materials", ("bancada", "granito", "marmore", "mármore", "quartzito", "cooktop", "pia", "ilha", "nicho", "cuba", "escada", "gourmet", "cozinha", "banheiro", "lavabo", "medicao", "medição")),
    ("robotics", ("robo", "robô", "robotica", "robótica", "limpeza", "xyron", "hygibot", "duno", "dune", "liro", "neobot", "escola", "educacional", "bncc")),
)

TOPIC_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("stairs", ("escada", "escadas")),
    ("gourmet", ("gourmet", "churrasqueira")),
    ("bathroom", ("banheiro", "lavabo", "nicho")),
    ("kitchen", ("cozinha", "bancada", "cooktop", "pia", "ilha")),
    ("quote_process", ("medicao", "medição", "medida", "fotos", "planta", "orcamento", "orçamento")),
    ("educational_robot", ("escola", "educacional", "professor", "aluno", "liro", "bncc")),
    ("cleaning_robot", ("limpeza", "duno", "dune", "hygibot")),
    ("security_robot", ("seguranca", "segurança", "patrulha", "orbit", "patrol", "vigilancia", "vigilância")),
    ("websites", ("site", "website", "loja virtual", "ecommerce", "django", "python")),
    ("robot_lineup", ("quais robos", "quais robôs", "quais modelos", "que robos", "que robôs", "linha xyron")),
    ("industrial_automation", ("mitsubishi", "clp", "ihm", "automacao", "automação")),
)

# Aplicação derivada (memória, sem enum no banco): desambigua "material/melhor/ele".
APPLICATION_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("stairs", ("escada", "escadas")),
    ("gourmet_countertop", ("gourmet", "churrasqueira")),
    ("bathroom_countertop", ("banheiro", "lavabo")),
    ("niche", ("nicho",)),
    ("cooktop_countertop", ("cooktop",)),
    ("kitchen_countertop", ("cozinha", "bancada", "pia", "ilha")),
    ("quote_process", ("medicao", "medição", "medida", "fotos", "planta")),
    ("educational_robotics", ("escola", "educacional", "professor", "aluno", "bncc")),
    ("cleaning_robotics", ("limpeza", "duno", "dune", "hygibot")),
    ("security_robotics", ("seguranca", "segurança", "patrulha", "orbit", "patrol", "vigilancia", "vigilância", "monitoramento")),
    ("industrial_automation", ("mitsubishi", "clp", "ihm")),
    ("websites", ("site", "website", "loja virtual", "django", "python")),
)

LINEUP_QUESTION_MARKERS = (
    "quais robos",
    "quais robôs",
    "quais modelos",
    "que robos voces",
    "que robôs vocês",
    "lista de robos",
    "lista de robôs",
)

STRONG_SWITCH_TOKENS = (
    "agora",
    "sobre sistemas",
    "python",
    "django",
    "mitsubishi",
    "site",
    "website",
    "loja virtual",
    "ecommerce",
    "bancada",
    "escada",
    "escadas",
    "gourmet",
    "banheiro",
    "medicao",
    "medição",
    "escola",
    "quais robos",
    "quais robôs",
)

PRONOUN_MARKERS = (
    r"\b(ele|ela|esse|essa|isto|isso|este|esta)\b",
    r"\besse (robo|robô|modelo|produto|sistema|material)\b",
    r"\beste (robo|robô|modelo|produto|sistema|material)\b",
    r"\bo (robo|robô|modelo|produto)\b",
)

CONTACT_DEFERRED_PATTERNS = (
    r"\bnao quero (passar|informar|dar|falar)\b.{0,40}\b(contato|telefone|whatsapp|e-?mail|email|celular)\b",
    r"\bprefiro nao (passar|informar|dar)\b.{0,40}\b(contato|telefone|whatsapp|e-?mail|email)\b",
    r"\bnao quero passar contato\b",
    r"\bdepois (eu )?(passo|envio|mando) (meu |os )?dados\b",
    r"\bprefiro tirar minhas duvidas\b",
    r"\btire minhas duvidas\b",
    r"\btira minhas duvidas\b",
    r"\bsem (passar |dar )?(contato|telefone|dados) por agora\b",
    r"\bagora nao( quero)? (passar|informar)\b.{0,30}\b(telefone|contato|email|e-mail|whatsapp)\b",
)

CONTINUE_CONSULTATIVE_MARKERS = (
    "tire minhas duvidas",
    "tira minhas duvidas",
    "prefiro tirar minhas duvidas",
    "quero tirar duvidas",
    "so quero tirar duvidas",
    "só quero tirar dúvidas",
)

EXPLICIT_SUBJECT_CHANGE_MARKERS = (
    "quero saber sobre",
    "quero saber do",
    "quero saber da",
    "gostaria de saber sobre",
    "gostaria de saber do",
    "gostaria de saber da",
    "queria saber sobre",
    "fale sobre",
    "me fale sobre",
    "me conte sobre",
    "antes disso",
    "mudando de assunto",
    "mas quero saber",
    "quero saber mais",
    "na verdade",
)


@dataclass(frozen=True)
class SlotCollectionContext:
    collection_active: bool = False
    expected_slot: str = ""
    is_slot_value: bool = False
    preserve_active_subject: bool = False


def build_collection_slot_context(*, conversation=None, lead_draft=None, message: str = "") -> SlotCollectionContext:
    """Infere se o turno atual preenche slot comercial — sem alterar lead."""
    lead = lead_draft
    if lead is None and conversation is not None:
        try:
            lead = conversation.lead_draft
        except Exception:
            lead = None
    if lead is None:
        return SlotCollectionContext()

    qd = dict(getattr(lead, "qualification_data", None) or {})
    collection_active = bool(qd.get("collection_active"))
    if not collection_active:
        return SlotCollectionContext()

    from leads.services.commercial import QualificationService

    pending = QualificationService().promptable_fields(lead, message=message)
    expected_slot = str(pending[0] if pending else "")
    if is_explicit_knowledge_subject_change(message):
        return SlotCollectionContext(
            collection_active=True,
            expected_slot=expected_slot,
            is_slot_value=False,
            preserve_active_subject=False,
        )

    is_slot = _message_fits_contact_slot_pattern(message, expected_slot=expected_slot)
    return SlotCollectionContext(
        collection_active=True,
        expected_slot=expected_slot,
        is_slot_value=is_slot,
        preserve_active_subject=is_slot,
    )


def is_explicit_knowledge_subject_change(message: str) -> bool:
    normalized = normalize_text(message)
    if not normalized:
        return False
    if any(marker in normalized for marker in EXPLICIT_SUBJECT_CHANGE_MARKERS):
        return True
    if detect_entity_mention(message) and any(
        marker in normalized
        for marker in ("quero", "saber", "fale", "conte", "sobre", "duvida", "dúvida", "informac", "informação")
    ):
        return True
    try:
        from assistant_core.discovery import analyze_message
        from assistant_core.services.decision_outcome import _is_product_information_discovery

        discovery = analyze_message(message)
        if _is_product_information_discovery(discovery):
            return True
    except Exception:
        pass
    return False


def _message_fits_contact_slot_pattern(message: str, *, expected_slot: str = "") -> bool:
    from assistant_core.qualification import (
        extract_contact_snapshot,
        infer_pending_field_values,
        is_valid_company,
        is_valid_email,
        is_valid_name,
        is_valid_phone,
        message_is_plausible_phone_candidate,
    )
    from assistant_core.qualification.livia import _extract_phone

    text = str(message or "").strip()
    if not text or "?" in text or len(text) > 120:
        return False
    if is_explicit_knowledge_subject_change(text):
        return False

    slots = [expected_slot] if expected_slot else []
    for slot in ("name", "phone", "email", "company", "need_summary"):
        if slot and slot not in slots:
            slots.append(slot)
    for slot in slots:
        if slot == "need_summary":
            from assistant_core.consultative_policy import _is_direct_need_slot_answer

            class _NeedLead:
                need_summary = ""

            if _is_direct_need_slot_answer(text, _NeedLead()):
                return True
            continue
        if slot and infer_pending_field_values(text, slot):
            return True

    snapshot = extract_contact_snapshot(text)
    if snapshot.email and is_valid_email(snapshot.email):
        return True
    if message_is_plausible_phone_candidate(text):
        phone = _extract_phone(text)
        if phone and is_valid_phone(phone):
            return True
        digits = re.sub(r"\D", "", text)
        if digits and is_valid_phone(digits):
            return True
    if is_valid_company(text):
        return True
    if is_valid_name(text) and 1 <= len(text.split()) <= 5:
        return True
    return False


def should_preserve_knowledge_subject(
    *,
    memory: DialogueMemory,
    message: str,
    slot_context: SlotCollectionContext | None = None,
) -> bool:
    if not memory.active_knowledge_subject:
        return False
    if is_explicit_knowledge_subject_change(message):
        return False
    slot_context = slot_context or SlotCollectionContext()
    if slot_context.preserve_active_subject:
        return True
    if slot_context.collection_active and _message_fits_contact_slot_pattern(
        message,
        expected_slot=slot_context.expected_slot,
    ):
        return True
    if _message_fits_contact_slot_pattern(message) and not message_has_pronoun_reference(message):
        confidence = float(memory.active_knowledge_subject.get("confidence") or 0.0)
        if confidence >= 0.55 and not detect_entity_mention(message):
            return True
    return False


@dataclass
class DialogueMemory:
    active_entity: str = ""
    active_domain: str = ""
    active_topic: str = ""
    active_application: str = ""
    active_knowledge_subject: dict = field(default_factory=dict)
    active_need: str = ""
    contact_collection_deferred: bool = False
    commercial_intent: bool = False
    collection_paused: bool = False
    entity_match: bool = False
    domain_match: bool = False
    retrieval_query_original: str = ""
    retrieval_query_contextual: str = ""
    notes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            ACTIVE_ENTITY_KEY: self.active_entity,
            ACTIVE_DOMAIN_KEY: self.active_domain,
            ACTIVE_TOPIC_KEY: self.active_topic,
            ACTIVE_APPLICATION_KEY: self.active_application,
            ACTIVE_KNOWLEDGE_SUBJECT_KEY: self.active_knowledge_subject,
            ACTIVE_NEED_KEY: self.active_need,
            CONTACT_DEFERRED_KEY: self.contact_collection_deferred,
            COMMERCIAL_INTENT_KEY: self.commercial_intent,
            COLLECTION_PAUSED_KEY: self.collection_paused,
        }

    def observability(self) -> dict:
        return {
            "active_entity": self.active_entity,
            "active_domain": self.active_domain,
            "active_topic": self.active_topic,
            "active_application": self.active_application,
            "active_knowledge_subject": self.active_knowledge_subject,
            "entity_match": self.entity_match,
            "domain_match": self.domain_match,
            "contextual_query_used": bool(
                self.retrieval_query_contextual
                and self.retrieval_query_contextual.strip()
                and self.retrieval_query_contextual.strip() != (self.retrieval_query_original or "").strip()
            ),
            "contact_collection_deferred": self.contact_collection_deferred,
            "collection_paused": self.collection_paused,
            "commercial_intent": self.commercial_intent,
        }


def load_dialogue_memory(conversation=None, lead_draft=None) -> DialogueMemory:
    lead = lead_draft
    if lead is None and conversation is not None:
        try:
            lead = conversation.lead_draft
        except Exception:
            lead = None
    data = {}
    if lead is not None:
        raw = getattr(lead, "qualification_data", None) or {}
        if isinstance(raw, dict):
            data = raw
    return DialogueMemory(
        active_entity=str(data.get(ACTIVE_ENTITY_KEY) or ""),
        active_domain=str(data.get(ACTIVE_DOMAIN_KEY) or ""),
        active_topic=str(data.get(ACTIVE_TOPIC_KEY) or ""),
        active_application=str(data.get(ACTIVE_APPLICATION_KEY) or ""),
        active_knowledge_subject=dict(data.get(ACTIVE_KNOWLEDGE_SUBJECT_KEY) or {}),
        active_need=str(data.get(ACTIVE_NEED_KEY) or getattr(lead, "need_summary", "") or ""),
        contact_collection_deferred=bool(data.get(CONTACT_DEFERRED_KEY)),
        commercial_intent=bool(data.get(COMMERCIAL_INTENT_KEY)),
        collection_paused=bool(data.get(COLLECTION_PAUSED_KEY)),
    )


def persist_dialogue_memory(lead_draft, memory: DialogueMemory) -> None:
    if lead_draft is None or not hasattr(lead_draft, "qualification_data"):
        return
    # Evita sobrescrever flags gravadas por outro caminho na mesma request (coleta).
    try:
        lead_draft.refresh_from_db(fields=["qualification_data"])
    except Exception:
        pass
    data = dict(getattr(lead_draft, "qualification_data", None) or {})
    preserved_active = bool(data.get("collection_active"))
    payload = memory.to_dict()
    if preserved_active and not memory.collection_paused:
        # Coleta explícita aberta: não reaplicar pause/deferência da memória em memória stale.
        payload.pop(COLLECTION_PAUSED_KEY, None)
        payload.pop(CONTACT_DEFERRED_KEY, None)
    data.update(payload)
    if preserved_active and not memory.collection_paused:
        data["collection_active"] = True
        data[COLLECTION_PAUSED_KEY] = False
    lead_draft.qualification_data = data
    update_fields = ["qualification_data"]
    if hasattr(lead_draft, "updated_at"):
        update_fields.append("updated_at")
    lead_draft.save(update_fields=update_fields)


def is_contact_deferred(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(re.search(pattern, normalized) for pattern in CONTACT_DEFERRED_PATTERNS)


def wants_consultative_continue(text: str) -> bool:
    normalized = normalize_text(text)
    return any(marker in normalized for marker in CONTINUE_CONSULTATIVE_MARKERS)


def message_has_pronoun_reference(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(re.search(pattern, normalized) for pattern in PRONOUN_MARKERS)


def detect_entity_mention(text: str) -> dict | None:
    normalized = normalize_text(text)
    if not normalized:
        return None
    best = None
    best_len = 0
    for entry in ENTITY_REGISTRY:
        for alias in entry["aliases"]:
            alias_n = normalize_text(alias)
            if not alias_n:
                continue
            if re.search(rf"(?<!\w){re.escape(alias_n)}(?!\w)", normalized):
                if len(alias_n) > best_len:
                    best = entry
                    best_len = len(alias_n)
    return best


def _marker_in_text(normalized: str, marker: str) -> bool:
    marker_n = normalize_text(marker)
    if not marker_n:
        return False
    if " " in marker_n:
        return marker_n in normalized
    return re.search(rf"(?<!\w){re.escape(marker_n)}(?!\w)", normalized) is not None


def infer_domain(text: str, *, fallback: str = "") -> str:
    normalized = normalize_text(text)
    if not normalized:
        return fallback
    for domain, markers in DOMAIN_MARKERS:
        if any(_marker_in_text(normalized, marker) for marker in markers):
            return domain
    return fallback


def infer_topic(text: str, *, fallback: str = "") -> str:
    normalized = normalize_text(text)
    if not normalized:
        return fallback
    for topic, markers in TOPIC_MARKERS:
        if any(_marker_in_text(normalized, marker) for marker in markers):
            return topic
    return fallback


def infer_application(text: str, *, fallback: str = "") -> str:
    normalized = normalize_text(text)
    if not normalized:
        return fallback
    for application, markers in APPLICATION_MARKERS:
        if any(_marker_in_text(normalized, marker) for marker in markers):
            return application
    return fallback


def is_material_recommendation_question(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    asks_material = any(token in normalized for token in ("material", "pedra", "granito", "marmore", "quartzito"))
    asks_best = any(token in normalized for token in ("melhor", "recomenda", "indic", "suger"))
    return asks_material and asks_best


def is_lineup_question(text: str) -> bool:
    normalized = normalize_text(text)
    return any(marker in normalized for marker in LINEUP_QUESTION_MARKERS)


def update_dialogue_memory_from_turn(
    *,
    memory: DialogueMemory,
    current_message: str,
    history=None,
    need_summary: str = "",
    commercial_trigger: bool = False,
    tenant=None,
    slot_context: SlotCollectionContext | None = None,
) -> DialogueMemory:
    message = str(current_message or "")
    normalized = normalize_text(message)
    slot_context = slot_context or SlotCollectionContext()
    preserve_subject = should_preserve_knowledge_subject(
        memory=memory,
        message=message,
        slot_context=slot_context,
    )
    if preserve_subject:
        memory.notes["slot_value_turn"] = True
        memory.notes["preserve_active_subject"] = True
    else:
        memory.notes.pop("slot_value_turn", None)
        memory.notes.pop("preserve_active_subject", None)
    history_blob = " ".join(
        str(item.get("content") or "")
        for item in (history or [])
        if item.get("role") == "user"
    )
    context_blob = " ".join([memory.active_need, need_summary, history_blob, message]).strip()

    query_topic = infer_topic(message) or infer_topic(context_blob) or memory.active_topic
    query_application = infer_application(message) or infer_application(context_blob) or memory.active_application

    if tenant is not None and not preserve_subject:
        try:
            from knowledge_base.rag.entity_catalog import resolve_knowledge_entity

            resolution = resolve_knowledge_entity(
                tenant=tenant,
                message=message,
                active_subject=memory.active_knowledge_subject or None,
                active_application=query_application,
                active_topic=query_topic,
            )
        except Exception:
            resolution = None
        if resolution is not None and getattr(resolution, "ambiguous", False):
            memory.notes["entity_ambiguity_options"] = list(getattr(resolution, "ambiguity_options", ()) or ())
        elif resolution is not None and getattr(resolution, "subject", None):
            subject = dict(resolution.subject or {})
            memory.active_knowledge_subject = subject
            memory.active_entity = str(subject.get("canonical_name") or memory.active_entity)
            memory.entity_match = True
            memory.notes.pop("entity_ambiguity_options", None)

    entity = None if preserve_subject else detect_entity_mention(message)
    if entity:
        memory.active_entity = entity["canonical"]
        memory.active_domain = entity["domain"]
        memory.active_topic = entity["topic"]
        memory.active_application = infer_application(message) or _application_from_topic(entity["topic"])
        memory.entity_match = True
    elif message_has_pronoun_reference(message) and memory.active_entity:
        memory.entity_match = True
    elif is_lineup_question(message):
        memory.active_entity = ""
        memory.active_domain = "robotics"
        memory.active_topic = "robot_lineup"
        memory.active_application = ""
    elif not preserve_subject:
        switched = infer_domain(message)
        topic = infer_topic(message)
        application = infer_application(message)
        strong_switch = any(_marker_in_text(normalized, token) for token in STRONG_SWITCH_TOKENS)
        previous_domain = memory.active_domain
        previous_topic = memory.active_topic

        if switched and (switched != previous_domain or strong_switch) and not message_has_pronoun_reference(message):
            memory.active_domain = switched
            if strong_switch or switched != previous_domain:
                if switched in {"software_web", "automation", "materials", "maintenance"} or strong_switch:
                    memory.active_entity = ""
            if topic:
                memory.active_topic = topic
            if application:
                memory.active_application = application

        if topic and topic != previous_topic and not message_has_pronoun_reference(message):
            memory.active_topic = topic
            if application:
                memory.active_application = application
            elif topic:
                memory.active_application = _application_from_topic(topic) or memory.active_application
            # Troca de tópico material/aplicação: não arrastar entity de outro contexto.
            if topic in {"stairs", "gourmet", "bathroom", "kitchen", "quote_process", "websites", "educational_robot", "industrial_automation"}:
                if memory.active_entity and topic != "cleaning_robot":
                    entity_topic = next(
                        (entry["topic"] for entry in ENTITY_REGISTRY if entry["canonical"] == memory.active_entity),
                        "",
                    )
                    if entity_topic and entity_topic != topic:
                        memory.active_entity = ""
        elif application and application != memory.active_application and not message_has_pronoun_reference(message):
            # Cooktop dentro de cozinha: refina application sem resetar domínio.
            memory.active_application = application

    if not memory.active_domain:
        memory.active_domain = infer_domain(message) or infer_domain(context_blob)
    if not memory.active_topic:
        memory.active_topic = infer_topic(message) or infer_topic(context_blob)
    if not memory.active_application:
        memory.active_application = (
            infer_application(message)
            or _application_from_topic(memory.active_topic)
            or infer_application(context_blob)
        )

    # Material recommendation herda application do contexto (cozinha+bancada+cooktop).
    if is_material_recommendation_question(message) and not infer_application(message):
        if memory.active_application in {"", "quote_process"}:
            memory.active_application = infer_application(context_blob) or memory.active_application
        if not memory.active_domain:
            memory.active_domain = "materials"
        if not memory.active_topic:
            memory.active_topic = {
                "kitchen_countertop": "kitchen",
                "cooktop_countertop": "kitchen",
                "gourmet_countertop": "gourmet",
                "bathroom_countertop": "bathroom",
                "stairs": "stairs",
            }.get(memory.active_application, memory.active_topic or "kitchen")

    if need_summary:
        memory.active_need = str(need_summary).strip()[:240]
    elif not memory.active_need and normalized:
        if any(token in normalized for token in ("quero", "gostaria", "preciso", "rob", "bancada", "site", "escola", "cozinha")):
            memory.active_need = message.strip()[:240]
    elif memory.active_need and any(token in normalized for token in ("cooktop", "bancada", "cozinha", "gourmet", "escada")):
        # Acumula sinais de aplicação na need sem estourar.
        merged = f"{memory.active_need} {message.strip()}".strip()
        memory.active_need = merged[:240]

    if commercial_trigger:
        memory.commercial_intent = True

    if is_contact_deferred(message) or wants_consultative_continue(message):
        memory.contact_collection_deferred = True
        memory.collection_paused = True

    memory.domain_match = bool(memory.active_domain)
    return memory


def _application_from_topic(topic: str) -> str:
    return {
        "stairs": "stairs",
        "gourmet": "gourmet_countertop",
        "bathroom": "bathroom_countertop",
        "kitchen": "kitchen_countertop",
        "quote_process": "quote_process",
        "educational_robot": "educational_robotics",
        "cleaning_robot": "cleaning_robotics",
        "security_robot": "security_robotics",
        "websites": "websites",
        "industrial_automation": "industrial_automation",
    }.get(str(topic or ""), "")


def build_contextual_retrieval_query(
    *,
    current_message: str,
    memory: DialogueMemory | None = None,
    history=None,
    need_summary: str = "",
) -> tuple[str, str]:
    """Retorna (query_original, query_contextual)."""
    original = str(current_message or "").strip()
    memory = memory or DialogueMemory()
    parts: list[str] = []
    original_n = normalize_text(original)
    lineup = is_lineup_question(original)

    subject_name = str((memory.active_knowledge_subject or {}).get("canonical_name") or "").strip()
    if subject_name and not lineup and memory.active_topic != "robot_lineup":
        parts.append(subject_name)
    if memory.active_entity and memory.active_entity != subject_name and not lineup and memory.active_topic != "robot_lineup":
        parts.append(memory.active_entity)
        entity = next((e for e in ENTITY_REGISTRY if e["canonical"] == memory.active_entity), None)
        if entity:
            if normalize_text(memory.active_entity) == "duno":
                parts.extend(["HygiBot", "Dune", "robô de limpeza"])
            parts.extend(list(entity.get("keywords") or [])[:3])

    topic_query = {
        "cleaning_robot": "robô de limpeza HygiBot Dune",
        "educational_robot": "LIRO Little Bot robótica educacional escola",
        "robot_lineup": "Xyron robótica de serviço LIRO HygiBot NeoBot Orbit Buddy",
        "websites": "sistemas web sites lojas virtuais Python Django integrações",
        "stairs": "escadas sob medida pedras naturais",
        "gourmet": "áreas gourmet bancadas churrasqueira",
        "bathroom": "banheiros lavabos nichos cubas",
        "kitchen": "bancadas de cozinha cooktop pia",
        "quote_process": "orçamento medidas fotos planta medição técnica",
        "industrial_automation": "automação industrial Mitsubishi CLP IHM",
    }.get(memory.active_topic or "", "")
    if topic_query:
        parts.append(topic_query)

    application_query = {
        "kitchen_countertop": "materiais para bancada de cozinha",
        "cooktop_countertop": "materiais para bancada de cozinha com cooktop",
        "gourmet_countertop": "materiais bancada área gourmet",
        "bathroom_countertop": "materiais bancada banheiro lavabo",
        "stairs": "escadas sob medida pedras naturais",
        "educational_robotics": "LIRO robótica educacional escola",
        "industrial_automation": "automação Mitsubishi CLP",
    }.get(memory.active_application or "", "")
    if application_query:
        parts.append(application_query)

    if memory.active_domain == "software_web" or any(_marker_in_text(original_n, m) for m in ("site", "loja virtual", "website", "django")):
        parts.append("sistemas web sites lojas virtuais Python")
    if memory.active_domain == "automation":
        parts.append("automação industrial Mitsubishi CLP")
    if memory.active_domain == "materials" and not topic_query and not application_query:
        parts.append(memory.active_need or "bancada pedras naturais")
    if is_material_recommendation_question(original):
        if memory.active_application in {"kitchen_countertop", "cooktop_countertop"}:
            parts.append("materiais para bancada de cozinha com cooktop granito mármore quartzito perguntas frequentes")
        elif memory.active_application == "gourmet_countertop":
            parts.append("materiais bancada área gourmet granito mármore")
        elif memory.active_application:
            parts.append("materiais pedras naturais granito mármore quartzito")
        else:
            parts.append("qual é a melhor pedra materiais granito mármore quartzito")

    # Últimos turnos user relevantes (não concatena tudo; evita sticky de tópico antigo).
    if not lineup and memory.active_topic not in {"stairs", "gourmet", "websites", "quote_process", "robot_lineup"}:
        recent_user = [
            str(item.get("content") or "").strip()
            for item in (history or [])[-6:]
            if item.get("role") == "user" and str(item.get("content") or "").strip()
        ]
        for turn in recent_user[-2:]:
            if message_has_pronoun_reference(turn):
                continue
            if _message_fits_contact_slot_pattern(turn):
                continue
            if len(turn.split()) <= 12:
                parts.append(turn)

    if need_summary and memory.active_topic not in {"websites", "stairs", "robot_lineup"}:
        parts.append(str(need_summary)[:80])

    skip_original = bool(memory.notes.get("slot_value_turn")) or _message_fits_contact_slot_pattern(original)
    if original and not skip_original and not (message_has_pronoun_reference(original) and len(original.split()) <= 6):
        parts.append(original)
    elif original and not skip_original:
        parts.append(original)

    # Dedup preservando ordem.
    seen: set[str] = set()
    ordered: list[str] = []
    for part in parts:
        key = normalize_text(part)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(part.strip())
    contextual = " ".join(ordered).strip() or original
    return original, contextual


def domain_followup(memory: DialogueMemory, *, force: bool = False) -> str:
    """Follow-up opcional por domínio. Vazio = não anexar pergunta."""
    if not force:
        return ""
    domain = memory.active_domain
    if domain == "robotics" or memory.active_topic == "cleaning_robot":
        return "Para encaixar melhor, qual é o ambiente e o tipo de piso?"
    if domain == "materials":
        return "Você já tem medidas aproximadas ou fotos do ambiente?"
    if domain == "automation":
        return "Qual ambiente e objetivo você quer cobrir primeiro?"
    if domain == "software_web":
        return "O foco é divulgação, captura de contatos, vendas online ou sistema interno?"
    return ""


def should_skip_consultative_followup(*, current_message: str, memory: DialogueMemory | None = None) -> bool:
    """Pedidos diretos de informação sobre entidade/tópico não devem forçar pergunta."""
    normalized = normalize_text(current_message)
    if not normalized:
        return False
    if any(marker in normalized for marker in ("fale sobre", "me fale", "me fala", "conte mais", "mais sobre", "tire minhas", "tira minhas")):
        return True
    if memory and memory.active_entity and message_has_pronoun_reference(current_message):
        return True
    if detect_entity_mention(current_message):
        return True
    return False
