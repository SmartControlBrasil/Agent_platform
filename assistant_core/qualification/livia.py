from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


INVALID_GENERIC_VALUES = {
    "",
    "nao informado",
    "não informado",
    "nao informada",
    "não informada",
    "sim",
    "ok",
    "quero",
    "quero sim",
    "gostaria",
    "pode ser",
    "orcamento",
    "orçamento",
    "quero orcamento",
    "quero orçamento",
    "preciso de orcamento",
    "preciso de orçamento",
    "atendimento",
    "quero atendimento",
    "teste",
    "test",
    "fulano",
    "ciclano",
    "beltrano",
    "cliente",
    "usuario",
    "usuário",
    "empresa",
    "minha empresa",
    "sem empresa",
    "nao tenho",
    "não tenho",
}

INVALID_NAME_SNIPPETS = (
    "empresa",
    "orcamento",
    "orçamento",
    "proposta",
    "sistema",
    "plataforma",
    "suporte",
    "atendimento",
    "erro",
    "problema",
    "automacao",
    "automação",
    "manutencao",
    "manutenção",
    "limpeza",
    "robo",
    "robô",
    "galpao",
    "galpão",
    "concreto",
    "porcelanato",
)

NEED_STATEMENT_MARKERS = (
    "preciso",
    "quero",
    "gostaria",
    "tenho interesse",
    "estou procurando",
)

PRODUCT_CONTEXT_SNIPPETS = (
    "robo",
    "robô",
    "robotica",
    "robótica",
    "limpeza",
    "educacional",
    "escola",
    "automacao",
    "automação",
    "orcamento",
    "orçamento",
    "cotacao",
    "cotação",
    "proposta",
    "site",
    "sistema",
    "depósito",
    "deposito",
    "galpao",
    "galpão",
)

INVALID_COMPANY_OR_CITY_SNIPPETS = (
    "quero contato",
    "quero atendimento",
    "quero orçamento",
    "quero orcamento",
    "preciso de suporte",
    "preciso de atendimento",
    "falar com especialista",
    "pode agendar",
    "sistema",
    "plataforma",
    "telefone",
    "whatsapp",
    "email",
    "e-mail",
    "todo o brasil",
    "brasil todo",
    "nacional",
    "orcamento",
    "orçamento",
    "proposta",
    "cotacao",
    "cotação",
    "loja virtual",
    "preciso",
    "gostaria",
    "vendo",
    "trabalho",
    "quanto tempo",
    "quanto custa",
)

INVALID_CITY_VALUES = {
    "brasil",
    "nacional",
    "todo brasil",
    "todo o brasil",
    "sistema",
    "app",
    "aplicativo",
    "empresa",
    "cliente",
    "clientes",
    "atendimento",
}

REPETITION_NOISE_PATTERNS = (
    r"\bja falei\b",
    r"\bjá falei\b",
    r"\bcomo falei\b",
    r"\beu ja falei\b",
    r"\beu já falei\b",
    r"\bconforme falei\b",
)

GENERIC_NEED_PHRASES = {
    "quero orcamento",
    "quero orçamento",
    "preciso de orcamento",
    "preciso de orçamento",
    "quanto custa",
    "valor",
    "proposta",
    "orcamento",
    "orçamento",
    "quero",
    "sim",
    "ok",
}

NEED_CONTEXT_KEYWORDS = (
    "preciso",
    "quero",
    "orçamento",
    "orcamento",
    "problema",
    "erro",
    "falha",
    "automação",
    "automacao",
    "automatizar",
    "sistema",
    "plataforma",
    "suporte",
    "manutenção",
    "manutencao",
    "integração",
    "integracao",
    "projeto",
    "desenvolver",
    "clp",
    "ihm",
    "inversor",
    "servo",
    "scada",
    "mitsubishi",
    "retrofit",
    "painel",
    "robo",
    "robô",
    "robotica",
    "robótica",
    "xyron",
    "liro",
    "hygibot",
    "esteira",
    "bike",
    "academia",
    "site",
    "loja",
    "virtual",
    "ecommerce",
    "e-commerce",
    "catalogo",
    "catálogo",
    "produtos",
    "portal",
    "aplicativo",
    "dashboard",
    "crm",
    "agente",
    "limpar",
    "limpeza",
    "galpao",
    "galpão",
    "fabrica",
    "fábrica",
    "escola",
    "condominio",
    "condomínio",
    "industria",
    "indústria",
    "recepcao",
    "recepção",
    "deposito",
    "depósito",
    "armazem",
    "armazém",
    # marmoraria / pedras naturais (Pitondo e verticais similares)
    "cozinha",
    "bancada",
    "pia",
    "cooktop",
    "ilha",
    "frontao",
    "frontão",
    "banheiro",
    "lavabo",
    "cuba",
    "nicho",
    "escada",
    "gourmet",
    "churrasqueira",
    "granito",
    "marmore",
    "mármore",
    "quartzito",
    "pedra",
    "marmoraria",
    "revestimento",
    "acabamento",
    "medidas",
    "planta",
)


@dataclass(frozen=True)
class ContactSnapshot:
    name: str = ""
    company: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""

    def has_any_contact(self) -> bool:
        return any([self.name, self.company, self.email, self.phone, self.city])


def normalize_text(value) -> str:
    normalized = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", normalized)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", normalized)


def strip_repetition_noise(value) -> str:
    cleaned = str(value or "").strip()
    for pattern in REPETITION_NOISE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip(" ,.-")


def is_generic_value(value) -> bool:
    return normalize_text(value) in {normalize_text(item) for item in INVALID_GENERIC_VALUES}


def is_valid_email(value) -> bool:
    if is_generic_value(value):
        return False
    return bool(re.fullmatch(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", str(value or "").strip(), re.IGNORECASE))


def is_valid_phone(value) -> bool:
    if is_generic_value(value):
        return False
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) in {12, 13} and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) not in {10, 11}:
        return False
    if len(set(digits)) <= 2 and digits[-4:] in {"0000", "1111", "9999"}:
        return digits in {"11999999999"}
    return True


def is_valid_name(value) -> bool:
    cleaned = strip_repetition_noise(value)
    normalized = normalize_text(cleaned)
    from assistant_core.continuity_policy import is_operational_collection_reply
    from leads.services.commercial import is_collection_deferral_phrase

    if is_collection_deferral_phrase(cleaned) or is_operational_collection_reply(cleaned):
        return False
    if is_generic_value(cleaned):
        return False
    if any(snippet in normalized for snippet in INVALID_NAME_SNIPPETS):
        return False
    if any(marker in normalized for marker in NEED_STATEMENT_MARKERS):
        return False
    from assistant_core.conversation_turns import looks_like_environment_answer

    if looks_like_environment_answer(normalized):
        return False
    if re.search(r"\d", cleaned):
        return False
    if len(normalized.split()) > 4:
        return False
    return bool(re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{1,119}", cleaned))


def is_valid_company(value) -> bool:
    cleaned = strip_repetition_noise(value)
    normalized = normalize_text(cleaned)
    from assistant_core.continuity_policy import is_operational_collection_reply
    from assistant_core.conversation_turns import looks_like_environment_answer
    from leads.services.commercial import is_collection_deferral_phrase

    if is_collection_deferral_phrase(cleaned) or is_operational_collection_reply(cleaned):
        return False
    if looks_like_environment_answer(normalized):
        return False
    if any(marker in normalized for marker in NEED_STATEMENT_MARKERS):
        return False
    if any(snippet in normalized for snippet in PRODUCT_CONTEXT_SNIPPETS):
        return False
    if any(marker in normalized for marker in OPERATIONAL_NUMBER_CONTEXT_MARKERS):
        return False
    if re.search(r"\b\d+\s*(?:m2|m²|metros?\s+quadrados?|funcionarios?|funcionários?|turnos?)\b", normalized):
        return False
    return _is_valid_company_or_city(cleaned, allow_numbers=True, invalid_values={"empresa", "minha empresa", "teste"})


def is_valid_city(value) -> bool:
    if normalize_text(value) in {normalize_text(item) for item in INVALID_CITY_VALUES}:
        return False
    return _is_valid_company_or_city(value, allow_numbers=False, invalid_values=INVALID_CITY_VALUES)


def is_valid_need_summary(value) -> bool:
    cleaned = strip_repetition_noise(value)
    normalized = normalize_text(cleaned)
    if not cleaned or normalized in {normalize_text(item) for item in GENERIC_NEED_PHRASES}:
        return False
    context_keywords = tuple(
        normalize_text(item)
        for item in NEED_CONTEXT_KEYWORDS
        if normalize_text(item) not in {"quero", "preciso", "orcamento", "orçamento"}
    )
    has_context = any(keyword in normalized for keyword in context_keywords) or bool(re.search(r"\bia\b", normalized))
    if not has_context:
        return False
    if len(cleaned) < 18 and not re.search(
        r"\b(?:site|loja|clp|ihm|robo|robô|ia|bancada|cozinha|banheiro|escada|granito|marmore|mármore|nicho|gourmet|galpao|galpão|limpar|limpeza|escola|fabrica|fábrica|condominio|condomínio|industria|indústria|recepcao|recepção|deposito|depósito|armazem|armazém)\b",
        normalized,
    ):
        return False
    return True


def message_fills_pending_slot(message: str, pending_field: str) -> bool:
    """Indica se a mensagem responde ao slot comercial pendente."""
    from assistant_core.services.decision_outcome import is_consultative_knowledge_message

    if is_consultative_knowledge_message(message):
        return False
    pending = str(pending_field or "").strip()
    if not pending:
        return False
    if pending == "company" and is_company_not_applicable(message):
        return True
    inferred = infer_pending_field_values(message, pending)
    if inferred:
        return True
    if pending == "need_summary":
        return is_valid_need_summary(message)
    if pending == "name_or_company":
        from assistant_core.conversation_turns import is_consultative_context_answer
        from leads.services.commercial import is_collection_deferral_phrase

        if is_consultative_context_answer(message) or is_collection_deferral_phrase(message):
            return False
        snapshot = extract_contact_snapshot(message)
        return bool(
            (snapshot.name and is_valid_name(snapshot.name))
            or (snapshot.company and is_valid_company(snapshot.company))
        )
    if pending == "phone_or_email":
        snapshot = extract_contact_snapshot(message)
        return bool(
            (snapshot.phone and is_valid_phone(snapshot.phone))
            or (snapshot.email and is_valid_email(snapshot.email))
        )
    return False


def minimum_lead_data_met(lead_draft) -> bool:
    return all(validator(getattr(lead_draft, field, "")) for field, validator in (
        ("name", is_valid_name), ("phone", is_valid_phone),
        ("email", is_valid_email), ("need_summary", is_valid_need_summary),
    ))


def is_company_not_applicable(message: str) -> bool:
    return normalize_text(message).strip(" .!;") in {
        "nao", "nao tenho empresa", "sou pessoa fisica", "e particular", "nao se aplica",
    }


def extract_contact_snapshot(text: str) -> ContactSnapshot:
    normalized = str(text or "").strip()
    if is_company_not_applicable(normalized):
        return ContactSnapshot()
    return ContactSnapshot(
        name=_extract_name(normalized),
        company=_extract_company(normalized),
        email=_extract_email(normalized),
        phone=_extract_phone(normalized),
        city=_extract_city(normalized),
    )


def infer_pending_field_values(message: str, pending_field: str) -> dict[str, str]:
    """Interpreta resposta curta ao próximo campo pedido (ex.: 'Maria Silva' após pedir nome)."""
    text = strip_repetition_noise(str(message or "").strip(" .,-"))
    pending = str(pending_field or "").strip()
    if not text or not pending or "?" in text or len(text) > 80:
        return {}

    from assistant_core.conversation_turns import is_consultative_context_answer, is_direct_question

    if pending == "need_summary" and is_consultative_context_answer(text):
        return {}

    normalized = normalize_text(text)
    from assistant_core.continuity_policy import is_operational_collection_reply

    if is_operational_collection_reply(text):
        return {}
    reject_markers = (
        "preciso",
        "quero",
        "gostaria",
        "orcamento",
        "orçamento",
        "proposta",
        "cotacao",
        "cotação",
        "quanto",
        "vendo",
        "trabalho",
        "site",
        "loja",
        "automat",
        "integr",
        "prazo",
        "tempo",
        "custa",
        "valor",
    )
    if pending == "name_or_company":
        from assistant_core.services.decision_outcome import is_consultative_knowledge_message
        from leads.services.commercial import is_collection_deferral_phrase

        if is_consultative_knowledge_message(text):
            return {}
        if is_collection_deferral_phrase(text):
            return {}
        if _extract_email(text) or _extract_phone(text):
            return {}
        bare = text.strip(" .,-")
        company_markers = ("empresa", "sou da", "trabalho na", "da empresa")
        has_company_marker = any(marker in normalized for marker in company_markers)
        if not has_company_marker and any(marker in normalized for marker in reject_markers):
            return {}
        name_candidate = re.sub(
            r"^(?:meu nome é|meu nome e|me chamo|sou o|sou a|sou|nome)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" .,-")
        company_candidate = re.sub(
            r"^(?:a empresa é|a empresa e|empresa|sou da|trabalho na|da)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip(" .,-")
        if has_company_marker:
            if is_valid_company(company_candidate) and len(company_candidate.split()) <= 6:
                return {"company": company_candidate}
            return {}
        company_from_pattern = _normalize_company_candidate(text)
        if company_from_pattern and is_valid_company(company_from_pattern):
            return {"company": company_from_pattern}
        if is_valid_name(name_candidate) and 1 <= len(name_candidate.split()) <= 2:
            return {"name": name_candidate}
        if (
            is_valid_company(company_candidate)
            and 1 <= len(company_candidate.split()) <= 6
            and not any(marker in normalize_text(company_candidate) for marker in reject_markers)
        ):
            return {"company": company_candidate}
        if is_valid_company(bare) and len(bare.split()) >= 3:
            return {"company": bare[:120]}
        if is_valid_name(name_candidate) and 1 <= len(name_candidate.split()) <= 4:
            return {"name": name_candidate}
        if is_valid_company(bare) and 1 <= len(bare.split()) <= 6:
            return {"company": bare[:120]}
        return {}

    if pending == "need_summary":
        from assistant_core.consultative_policy import is_explicit_human_handoff

        if is_explicit_human_handoff(text) or is_direct_question(text):
            return {}
        cleaned = text.strip(" .,-")
        if is_valid_need_summary(cleaned):
            return {"need_summary": cleaned[:500]}
        return {}

    if pending == "phone_or_email":
        if not message_is_plausible_phone_candidate(text) and not _extract_email(text):
            return {}
        email = _extract_email(text)
        if email and is_valid_email(email):
            return {"email": email}
        if message_is_plausible_phone_candidate(text):
            phone = _extract_phone(text)
            if phone and is_valid_phone(phone):
                return {"phone": phone}
            digits = re.sub(r"\D", "", text)
            if digits and is_valid_phone(digits):
                return {"phone": digits}
        return {}

    if pending in {"name", "company", "email", "phone", "city"}:
        from assistant_core.services.decision_outcome import is_consultative_knowledge_message
        from leads.services.commercial import is_collection_deferral_phrase
        if is_consultative_knowledge_message(text) or is_collection_deferral_phrase(text) or is_consultative_context_answer(text):
            return {}
        if pending in {"name", "company"}:
            if is_company_not_applicable(text) or _extract_email(text) or _extract_phone(text):
                return {}
            if pending == "name":
                bare = text.strip(" .,-")
                name_intro_markers = ("meu nome e", "meu nome é", "me chamo", "sou o", "sou a")
                explicit_name = _extract_name(text)
                company_from_text = _extract_company(text) or _normalize_company_candidate(text)
                if company_from_text and is_valid_company(company_from_text):
                    result = {"company": company_from_text[:120]}
                    if pending == "name" and is_valid_name(company_from_text):
                        result["name"] = company_from_text[:120]
                    return result
                if (
                    not explicit_name
                    and not any(marker in normalized for marker in name_intro_markers)
                    and len(bare.split()) >= 3
                    and is_valid_company(bare)
                ):
                    result = {"company": bare[:120]}
                    if pending == "name" and is_valid_name(bare):
                        result["name"] = bare[:120]
                    return result
            if any(marker in normalized for marker in reject_markers):
                return {}
        if pending == "phone" and not message_is_plausible_phone_candidate(text):
            return {}
        mapped = {
            "name": ("name", _extract_name(text) or text),
            "company": ("company", _extract_company(text) or text),
            "email": ("email", _extract_email(text)),
            "phone": ("phone", _extract_phone(text) or re.sub(r"\D", "", text)),
            "city": ("city", _extract_city(text) or text),
        }
        field_name, value = mapped[pending]
        validators = {
            "name": is_valid_name,
            "company": is_valid_company,
            "email": is_valid_email,
            "phone": is_valid_phone,
            "city": is_valid_city,
        }
        candidate = str(value or "").strip()
        if field_name in {"name", "company"} and any(marker in normalize_text(candidate) for marker in reject_markers):
            return {}
        if candidate and validators[field_name](candidate):
            if pending == "name" and field_name == "name" and not _extract_name(text):
                if len(candidate.split()) >= 3 and is_valid_company(candidate):
                    return {"company": candidate[:120]}
            return {field_name: candidate}
    return {}


def has_basic_contact(text: str) -> bool:
    return extract_contact_snapshot(text).has_any_contact()


def looks_like_invalid_email(text: str) -> bool:
    raw = str(text or "")
    if is_valid_email(_extract_email(raw)):
        return False
    return bool(re.search(r"\b(?:email|e-mail|mail)\b", raw, re.IGNORECASE) or re.search(r"\S+@\S*", raw))


OPERATIONAL_NUMBER_CONTEXT_MARKERS = (
    "metro",
    "metragem",
    "m2",
    "m²",
    "metro quadrado",
    "metros quadrados",
    "funcionario",
    "funcionários",
    "funcionarios",
    "turno",
    "turnos",
    "hora",
    "horas",
    "24 horas",
    "galpao",
    "galpão",
    "area",
    "área",
    "ambiente",
    "piso",
    "epoxi",
    "epóxi",
    "concreto",
    "operacao",
    "operação",
    "industria",
    "indústria",
    "fabrica",
    "fábrica",
    "armazem",
    "armazém",
    "deposito",
    "depósito",
)

PHONE_EXPLICIT_MARKERS = (
    "telefone",
    "whatsapp",
    "celular",
    "fone",
    "zap",
    "ddd",
    "me liga",
    "me ligue",
)


def message_is_plausible_phone_candidate(text: str) -> bool:
    """Telefone só é candidato forte quando a mensagem tem forma ou marcador compatível."""
    raw = str(text or "").strip()
    if not raw:
        return False
    normalized = normalize_text(raw)
    digits = re.sub(r"\D", "", raw)
    has_phone_marker = any(marker in normalized for marker in PHONE_EXPLICIT_MARKERS)
    has_operational_context = any(marker in normalized for marker in OPERATIONAL_NUMBER_CONTEXT_MARKERS)
    if has_operational_context and not has_phone_marker:
        return False
    if is_valid_phone(digits):
        return True
    if has_phone_marker and digits:
        return True
    if re.search(r"\(?\d{2}\)?\s*\d{4,5}[\s.-]?\d{4}", raw):
        return True
    if re.search(r"\+?\d{2,3}[\s.-]?\(?\d{2}\)?", raw) and len(digits) >= 10:
        return True
    if re.fullmatch(r"[\d\s().+-]+", raw) and len(digits) in {10, 11}:
        return True
    if re.fullmatch(r"[\d\s().+-]+", raw) and 3 <= len(digits) <= 9:
        return True
    return False


def looks_like_invalid_phone(text: str) -> bool:
    if not message_is_plausible_phone_candidate(text):
        return False
    raw = str(text or "")
    digits = re.sub(r"\D", "", raw)
    if is_valid_phone(digits):
        return False
    return True


def _extract_email(text: str) -> str:
    match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.IGNORECASE)
    return match.group(0) if match else ""


def _extract_phone(text: str) -> str:
    match = re.search(r"(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?\d{4,5}[\s.-]?\d{4}", text)
    if not match:
        return ""
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) in {12, 13} and digits.startswith("55"):
        digits = digits[2:]
    return digits


def _normalize_company_candidate(text: str) -> str:
    raw = " ".join(str(text or "").strip().split())
    normalized = normalize_text(raw)
    if not normalized:
        return ""
    if normalized.startswith("grupo") and len(normalized) > 5:
        if " " in raw:
            return raw
        suffix = normalized[5:].strip()
        if len(suffix) >= 3:
            return f"Grupo {suffix.title()}"
    if re.search(r"\b(ltda|s\.?a\.?|me|epp|industria|indústria)\b", normalized):
        return raw[:120]
    return ""


def _extract_name(text: str) -> str:
    match = re.search(r"(?:meu nome e|meu nome é|sou)\s*[:\-]?\s*([^,;]+)", text, re.IGNORECASE)
    if not match:
        return ""
    candidate = match.group(1).strip()
    candidate = re.split(r"\s+[eE]\s+(?:meu|minha|meus|minhas|preciso|quero)\b", candidate, maxsplit=1)[0].strip()
    candidate = re.split(r"[.!?]\s*(?:preciso|quero|tenho|busco)\b", candidate, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return candidate


def _extract_company(text: str) -> str:
    match = re.search(r"(?:empresa|companhia)\s*(?:é|e|:)?\s+([^,;]+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(
        r"\b(?:da|de|do)\s+(?!um\b|uma\b|uns\b|umas\b|o\b|a\b)([A-ZÀ-Ý][A-Za-zÀ-ÿ0-9 .&'-]{1,60})",
        text,
    )
    if not match:
        return ""
    candidate = match.group(1).strip()
    if re.match(r"^\d", candidate):
        return ""
    words = candidate.split()
    if len(words) > 6:
        candidate = " ".join(words[:6])
    return candidate


def _extract_city(text: str) -> str:
    match = re.search(r"(?:cidade|sou de|estou em|fica em)\s*[:\-]?\s*(.+)$", text, re.IGNORECASE)
    if not match:
        return ""
    return re.split(r"[,;]", match.group(1).strip(), maxsplit=1)[0].strip()


def _is_valid_company_or_city(value, *, allow_numbers: bool, invalid_values) -> bool:
    cleaned = strip_repetition_noise(value)
    normalized = normalize_text(cleaned)
    invalid_normalized = {normalize_text(item) for item in invalid_values}
    if is_generic_value(cleaned) or normalized in invalid_normalized:
        return False
    if any(snippet in normalized for snippet in tuple(normalize_text(item) for item in INVALID_COMPANY_OR_CITY_SNIPPETS)):
        return False
    if normalized.startswith("de ") or normalized.startswith("da ") or normalized.startswith("do "):
        return False
    digits = re.sub(r"\D", "", cleaned)
    if not allow_numbers and digits:
        return False
    if len(digits) >= 8:
        return False
    pattern = r"[A-Za-z0-9À-ÿ][A-Za-z0-9À-ÿ .&/'-]{1,159}" if allow_numbers else r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{1,119}"
    return bool(re.fullmatch(pattern, cleaned))
