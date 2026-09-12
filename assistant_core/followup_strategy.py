"""Seleção centralizada de follow-up consultivo por domínio/aplicação."""

from __future__ import annotations

from assistant_core.conversation_turns import normalize_text
from assistant_core.dialogue_memory import DialogueMemory, should_skip_consultative_followup


def select_followup(
    *,
    memory: DialogueMemory | None = None,
    current_message: str = "",
    need_summary: str = "",
    answer_text: str = "",
    history=None,
    force: bool = False,
) -> tuple[str, dict]:
    """
    Retorna (followup, diagnostics).

    Follow-up é opcional: string vazia quando a resposta já basta ou o turno pede continuidade informativa.
    """
    memory = memory or DialogueMemory()
    diagnostics = {
        "followup_selected": False,
        "followup_domain": memory.active_domain or "",
        "followup_application": getattr(memory, "active_application", "") or "",
        "followup_strategy": "none",
    }
    if "?" in answer_text:
        diagnostics["followup_strategy"] = "skipped_existing_question"
        return "", diagnostics
    if not force and should_skip_consultative_followup(current_message=current_message, memory=memory):
        diagnostics["followup_strategy"] = "skipped_direct_ask"
        return "", diagnostics
    if not force and _answer_already_sufficient(answer_text, current_message):
        diagnostics["followup_strategy"] = "skipped_sufficient"
        return "", diagnostics

    domain = memory.active_domain or ""
    topic = memory.active_topic or ""
    application = getattr(memory, "active_application", "") or ""
    blob = normalize_text(" ".join([current_message, topic, domain, application, need_summary, memory.active_entity]))

    follow = ""
    if domain == "automation" or topic == "industrial_automation" or "mitsubishi" in blob or "clp" in blob:
        follow = "Qual equipamento ou processo você precisa automatizar?"
        diagnostics["followup_domain"] = "automation"
    elif topic == "educational_robot" or application == "educational_robotics" or (
        "escola" in blob and "limpeza" not in blob
    ):
        follow = "Qual é a faixa de ensino ou o objetivo pedagógico principal?"
        diagnostics["followup_domain"] = "robotics"
    elif topic == "cleaning_robot" or application == "cleaning_robotics" or any(
        token in blob for token in ("duno", "dune", "hygibot", "limpeza")
    ):
        from assistant_core.consultative_slots import (
            extract_consultative_slots,
            select_cleaning_followup,
            should_skip_followup_for_answered_slots,
        )

        slots = extract_consultative_slots(
            need_summary=need_summary,
            history=history,
            current_message=current_message,
        )
        follow = select_cleaning_followup(slots=slots, current_message=current_message)
        if should_skip_followup_for_answered_slots(
            follow,
            need_summary=need_summary,
            history=history,
            current_message=current_message,
        ):
            follow = ""
        diagnostics["followup_domain"] = "robotics"
    elif domain == "software_web" or topic == "websites":
        if any(token in blob for token in ("loja virtual", "ecommerce", "e-commerce")):
            follow = "Você pretende começar com poucos produtos ou já tem um catálogo maior?"
        else:
            follow = "O foco é divulgação, captura de contatos ou outro objetivo do site?"
        diagnostics["followup_domain"] = "software_web"
    elif application == "stairs" or topic == "stairs" or "escada" in blob:
        follow = "Você já tem medidas, fotos ou planta da escada?"
        diagnostics["followup_domain"] = "materials"
    elif application in {"gourmet_countertop"} or topic == "gourmet" or "gourmet" in blob:
        follow = "A área gourmet já tem projeto ou medidas aproximadas?"
        diagnostics["followup_domain"] = "materials"
    elif application in {"bathroom_countertop", "niche"} or topic == "bathroom":
        follow = "O projeto já inclui bancada, cuba e nicho, ou só parte disso?"
        diagnostics["followup_domain"] = "materials"
    elif application in {"kitchen_countertop", "cooktop_countertop"} or topic == "kitchen":
        follow = "Você já tem medidas aproximadas ou fotos da bancada?"
        diagnostics["followup_domain"] = "materials"
    elif topic == "quote_process" or application == "quote_process":
        follow = ""
        diagnostics["followup_strategy"] = "skipped_process"
        return "", diagnostics
    elif domain == "materials":
        follow = "Você já tem medidas aproximadas ou fotos do ambiente?"
        diagnostics["followup_domain"] = "materials"
    elif domain == "robotics":
        follow = "Qual ambiente e objetivo você quer cobrir primeiro?"
        diagnostics["followup_domain"] = "robotics"

    if follow and follow.lower() not in normalize_text(answer_text):
        diagnostics["followup_selected"] = True
        diagnostics["followup_strategy"] = "domain_followup"
        return follow, diagnostics
    diagnostics["followup_strategy"] = "none"
    return "", diagnostics


def _answer_already_sufficient(answer_text: str, current_message: str) -> bool:
    text = str(answer_text or "").strip()
    if not text:
        return False
    if text.endswith("?"):
        return True
    # Resposta já cobre pedido informativo com 2+ sentenças úteis.
    sentences = [s.strip() for s in text.replace("!", ".").split(".") if len(s.strip()) > 25]
    if len(sentences) >= 2 and len(text) >= 160:
        msg = normalize_text(current_message)
        if any(token in msg for token in ("me fale", "fale mais", "como funciona", "trabalham com", "voces tem", "vocês têm")):
            return True
    return False
