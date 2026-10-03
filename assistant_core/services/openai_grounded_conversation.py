"""Serviço único de conversação grounded via OpenAI (camada principal de linguagem)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from django.conf import settings

from assistant_core.prompts.openai_conversation import (
    build_commercial_state_context,
    build_openai_conversation_prompt,
)
from assistant_core.services.ai_telemetry import record_ai_usage
from integrations.openai.client import OpenAIChatClient, OpenAIChatResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenAIConversationResult:
    text: str = ""
    used: bool = False
    status: str = "skipped"
    skip_reason: str = ""
    latency_ms: int = 0
    error_type: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    grounded: bool = False
    metadata: dict = field(default_factory=dict)


class OpenAIGroundedConversationService:
    """Gera texto de resposta via OpenAI; não muta estado comercial."""

    OPERATION = "openai_conversation"

    def __init__(self, ai_client: OpenAIChatClient | None = None):
        self.ai_client = ai_client or OpenAIChatClient()

    def generate(
        self,
        *,
        tenant,
        assistant_profile,
        message: str,
        conversation,
        discovery,
        decision,
        knowledge_context: str,
        history: list[dict[str, str]] | None = None,
        dialogue_memory=None,
        knowledge_result=None,
        deterministic_reply: str = "",
    ) -> OpenAIConversationResult:
        commercial_state = build_commercial_state_context(conversation=conversation, decision=decision)
        decision_action = str(commercial_state.get("action") or "ANSWER")
        rag_docs = _extract_rag_doc_refs(knowledge_context, knowledge_result, dialogue_memory)
        grounded = bool(str(knowledge_context or "").strip())

        started = time.monotonic()
        model = str(getattr(settings, "LIVIA_OPENAI_MODEL", "") or "gpt-4.1-mini")
        logger.info(
            "ai.conversation.started tenant_slug=%s grounded=%s collection_active=%s rag_docs=%s",
            getattr(tenant, "slug", ""),
            grounded,
            commercial_state.get("collection_active"),
            ",".join(rag_docs) or "-",
        )

        try:
            messages = build_openai_conversation_prompt(
                tenant=tenant,
                assistant_profile=assistant_profile,
                message=message,
                conversation=conversation,
                discovery_result=discovery,
                knowledge_context=knowledge_context,
                dialogue_memory=dialogue_memory,
                commercial_state=commercial_state,
                deterministic_reply=deterministic_reply or str(getattr(decision, "reply", "") or ""),
                history=list(history or []),
            )
            result: OpenAIChatResult = self.ai_client.create_chat_completion(messages=messages)
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "ai.conversation.failed tenant_slug=%s error_type=%s latency_ms=%s",
                getattr(tenant, "slug", ""),
                exc.__class__.__name__,
                latency_ms,
            )
            record_ai_usage(
                tenant=tenant,
                operation=self.OPERATION,
                model=model,
                success=False,
                latency_ms=latency_ms,
                error_type=exc.__class__.__name__,
                metadata={
                    "source": "openai_grounded_conversation",
                    "grounded": grounded,
                    "rag_docs": rag_docs,
                    "collection_active": commercial_state.get("collection_active"),
                    "decision_action": decision_action,
                    "fallback_used": True,
                },
            )
            return OpenAIConversationResult(
                status="failed",
                skip_reason="provider_exception",
                latency_ms=latency_ms,
                error_type=exc.__class__.__name__,
                grounded=grounded,
                metadata={"rag_docs": rag_docs, "collection_active": commercial_state.get("collection_active"), "decision_action": decision_action},
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        model = result.model or model

        if not result.success or not result.text:
            logger.warning(
                "ai.conversation.failed tenant_slug=%s error_type=%s latency_ms=%s",
                getattr(tenant, "slug", ""),
                result.error_type or "empty_response",
                latency_ms,
            )
            record_ai_usage(
                tenant=tenant,
                operation=self.OPERATION,
                model=model,
                success=False,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                latency_ms=latency_ms,
                error_type=result.error_type or "empty_response",
                metadata={
                    "source": "openai_grounded_conversation",
                    "grounded": grounded,
                    "rag_docs": rag_docs,
                    "collection_active": commercial_state.get("collection_active"),
                    "decision_action": decision_action,
                    "fallback_used": True,
                },
            )
            return OpenAIConversationResult(
                status="failed",
                skip_reason=result.error_type or "empty_response",
                latency_ms=latency_ms,
                error_type=result.error_type,
                model=model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                grounded=grounded,
                metadata={"rag_docs": rag_docs, "collection_active": commercial_state.get("collection_active"), "decision_action": decision_action},
            )

        logger.info(
            "ai.conversation.completed tenant_slug=%s latency_ms=%s chars=%s tokens=%s grounded=%s",
            getattr(tenant, "slug", ""),
            latency_ms,
            len(result.text),
            result.total_tokens,
            grounded,
        )
        record_ai_usage(
            tenant=tenant,
            operation=self.OPERATION,
            model=model,
            success=True,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            latency_ms=latency_ms,
            metadata={
                "source": "openai_grounded_conversation",
                "grounded": grounded,
                "rag_docs": rag_docs,
                "collection_active": commercial_state.get("collection_active"),
                "fallback_used": False,
            },
        )
        return OpenAIConversationResult(
            text=result.text.strip(),
            used=True,
            status="completed",
            latency_ms=latency_ms,
            model=model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            grounded=grounded,
            metadata={
                "rag_docs": rag_docs,
                "collection_active": commercial_state.get("collection_active"),
                "decision_action": decision_action,
                "provider": "openai",
            },
        )


def _extract_rag_doc_refs(knowledge_context: str, knowledge_result, dialogue_memory) -> list[str]:
    refs: list[str] = []
    if knowledge_result is not None:
        extras = getattr(knowledge_result, "extras", {}) or {}
        for key in ("source_document_ids", "manifest_ids", "chunk_ids"):
            raw = extras.get(key)
            if isinstance(raw, (list, tuple)):
                refs.extend(str(item) for item in raw if str(item).strip())
    if dialogue_memory is not None:
        subject = getattr(dialogue_memory, "active_knowledge_subject", {}) or {}
        for item in subject.get("source_document_ids") or []:
            refs.append(str(item))
    if not refs and knowledge_context.strip():
        refs.append("context_present")
    # dedupe preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for item in refs:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique[:10]
