"""Classificação semântica de intenção comercial — decisão estruturada, sem autoridade de domínio."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from django.conf import settings

from integrations.openai.client import OpenAIChatClient, OpenAIChatResult

logger = logging.getLogger(__name__)

INTENT_SOURCE_KEY = "intent_source"
INTENT_CONFIDENCE_KEY = "intent_confidence"
SEMANTIC_INTENT_REASON_KEY = "semantic_intent_reason"

INTENT_SOURCE_DETERMINISTIC = "deterministic"
INTENT_SOURCE_SEMANTIC = "semantic_classifier"


class CommercialIntentLabel(str, Enum):
    TECHNICAL_QUESTION = "technical_question"
    COMMERCIAL_INTEREST = "commercial_interest"
    QUOTE_REQUEST = "quote_request"
    HUMAN_HANDOFF = "human_handoff"
    OTHER = "other"


class SuggestedCommercialAction(str, Enum):
    CONTINUE_CONVERSATION = "continue_conversation"
    START_COLLECTION = "start_collection"
    REQUEST_HANDOFF = "request_handoff"


@dataclass(frozen=True)
class SemanticCommercialIntentDecision:
    intent: CommercialIntentLabel
    commercial_intent: bool
    confidence: float
    reason: str
    suggested_action: SuggestedCommercialAction
    available: bool = True
    error_type: str = ""


@dataclass(frozen=True)
class SemanticClassifierObservability:
    tenant_id: int | None
    conversation_id: int | None
    duration_ms: int
    success: bool
    intent: str
    confidence: float
    commercial_intent: bool
    error_type: str
    model: str


def is_semantic_intent_enabled() -> bool:
    if not bool(getattr(settings, "LIVIA_SEMANTIC_INTENT_ENABLED", False)):
        return False
    if not bool(getattr(settings, "LIVIA_AI_ENABLED", False)):
        return False
    if bool(getattr(settings, "LIVIA_AI_DRY_RUN", True)):
        return False
    api_key = str(getattr(settings, "LIVIA_OPENAI_API_KEY", "") or "").strip()
    return bool(api_key)


def _min_confidence() -> float:
    return float(getattr(settings, "LIVIA_SEMANTIC_INTENT_MIN_CONFIDENCE", 0.65) or 0.65)


def _redact_pii(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email]", cleaned)
    cleaned = re.sub(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?\d{4,5}[-\s]?\d{4}\b", "[telefone]", cleaned)
    return cleaned


def build_classifier_context(
    *,
    current_message: str,
    history: Iterable[dict[str, str]] | None = None,
    need_summary: str = "",
    collection_active: bool = False,
    commercial_intent_known: bool = False,
) -> str:
    lines = [
        f"collection_active: {bool(collection_active)}",
        f"commercial_intent_known: {bool(commercial_intent_known)}",
    ]
    if need_summary.strip():
        lines.append(f"need_summary: {_redact_pii(need_summary)[:400]}")
    recent = list(history or [])[-6:]
    for item in recent:
        role = str(item.get("role") or "user")
        content = _redact_pii(str(item.get("content") or ""))[:500]
        if content:
            lines.append(f"{role}: {content}")
    lines.append(f"current_message: {_redact_pii(current_message)[:800]}")
    return "\n".join(lines)


def parse_semantic_classifier_payload(raw: str) -> SemanticCommercialIntentDecision | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    intent_raw = str(data.get("intent") or "").strip().lower()
    try:
        intent = CommercialIntentLabel(intent_raw)
    except ValueError:
        return None
    action_raw = str(data.get("suggested_action") or "").strip().lower()
    try:
        suggested_action = SuggestedCommercialAction(action_raw)
    except ValueError:
        return None
    commercial_intent = bool(data.get("commercial_intent"))
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason") or "").strip()[:240]
    if not reason:
        return None
    return SemanticCommercialIntentDecision(
        intent=intent,
        commercial_intent=commercial_intent,
        confidence=confidence,
        reason=reason,
        suggested_action=suggested_action,
        available=True,
    )


class SemanticCommercialIntentService:
    """Chama OpenAI para classificar intenção; não persiste nem muta domínio."""

    def __init__(self, ai_client: OpenAIChatClient | None = None):
        self.ai_client = ai_client or OpenAIChatClient()

    def classify(
        self,
        *,
        current_message: str,
        history: Iterable[dict[str, str]] | None = None,
        need_summary: str = "",
        collection_active: bool = False,
        commercial_intent_known: bool = False,
        tenant_id: int | None = None,
        conversation_id: int | None = None,
    ) -> SemanticCommercialIntentDecision:
        if not is_semantic_intent_enabled():
            return SemanticCommercialIntentDecision(
                intent=CommercialIntentLabel.OTHER,
                commercial_intent=False,
                confidence=0.0,
                reason="",
                suggested_action=SuggestedCommercialAction.CONTINUE_CONVERSATION,
                available=False,
                error_type="disabled",
            )
        context = build_classifier_context(
            current_message=current_message,
            history=history,
            need_summary=need_summary,
            collection_active=collection_active,
            commercial_intent_known=commercial_intent_known,
        )
        system = (
            "Você classifica intenção comercial em conversas B2B/B2C de automação e serviços. "
            "Responda SOMENTE JSON válido com as chaves: intent, commercial_intent, confidence, reason, suggested_action. "
            "intent deve ser um de: technical_question, commercial_interest, quote_request, human_handoff, other. "
            "suggested_action deve ser um de: continue_conversation, start_collection, request_handoff. "
            "commercial_intent=true apenas quando há necessidade própria, projeto real, problema existente, "
            "busca por fornecedor, intenção de avaliação/contratação/proposta/preço/contato humano comercial. "
            "Perguntas puramente educacionais ou institucionais genéricas (ex.: o que é CLP, vocês trabalham com X) "
            "devem ser technical_question ou other com commercial_intent=false. "
            "reason: frase curta auditável, sem chain-of-thought."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": context},
        ]
        started = time.monotonic()
        result: OpenAIChatResult = self.ai_client.create_json_chat_completion(messages=messages)
        duration_ms = int((time.monotonic() - started) * 1000)
        obs = SemanticClassifierObservability(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            duration_ms=duration_ms,
            success=bool(result.success),
            intent="",
            confidence=0.0,
            commercial_intent=False,
            error_type=str(result.error_type or ""),
            model=str(result.model or ""),
        )
        if not result.success:
            logger.info(
                "semantic_intent_unavailable tenant_id=%s conversation_id=%s duration_ms=%s error_type=%s model=%s",
                tenant_id,
                conversation_id,
                duration_ms,
                result.error_type,
                result.model,
            )
            return SemanticCommercialIntentDecision(
                intent=CommercialIntentLabel.OTHER,
                commercial_intent=False,
                confidence=0.0,
                reason="",
                suggested_action=SuggestedCommercialAction.CONTINUE_CONVERSATION,
                available=False,
                error_type=str(result.error_type or "unavailable"),
            )
        parsed = parse_semantic_classifier_payload(result.text)
        if parsed is None:
            logger.info(
                "semantic_intent_invalid_json tenant_id=%s conversation_id=%s duration_ms=%s model=%s",
                tenant_id,
                conversation_id,
                duration_ms,
                result.model,
            )
            return SemanticCommercialIntentDecision(
                intent=CommercialIntentLabel.OTHER,
                commercial_intent=False,
                confidence=0.0,
                reason="",
                suggested_action=SuggestedCommercialAction.CONTINUE_CONVERSATION,
                available=False,
                error_type="invalid_json",
            )
        logger.info(
            "semantic_intent_classified tenant_id=%s conversation_id=%s duration_ms=%s intent=%s "
            "commercial_intent=%s confidence=%s model=%s",
            tenant_id,
            conversation_id,
            duration_ms,
            parsed.intent.value,
            parsed.commercial_intent,
            parsed.confidence,
            result.model,
        )
        return parsed

    def should_act_on(self, decision: SemanticCommercialIntentDecision) -> bool:
        if not decision.available:
            return False
        if not decision.commercial_intent:
            return False
        if decision.confidence < _min_confidence():
            return False
        if decision.suggested_action == SuggestedCommercialAction.CONTINUE_CONVERSATION:
            return False
        return True
