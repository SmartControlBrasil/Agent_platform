from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, connection, transaction

from assistant_core.discovery import analyze_message
from assistant_core.prompts.livia import DEFAULT_REPLY
from assistant_core.services.deterministic_synthesis import is_generic_fallback_reply
from conversations.models import Conversation, HandoffRequest, Message
from knowledge_base.rag.context_builder import KnowledgeContextResult, build_knowledge_context, build_knowledge_context_result
from tenants.services.human_handoff import build_human_handoff_payload

from .chat_idempotency import complete_chat_request, update_completed_chat_request_response
from .livia_decision import LiviaDecisionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DeterministicChatResult:
    chat_request: object
    tenant: object
    conversation: Conversation
    assistant_profile: object | None
    history: list[dict]
    user_message: str
    decision: object
    assistant_message: Message
    response_payload: dict
    dialogue_memory: object | None = None
    knowledge_result: KnowledgeContextResult | None = None


def process_chat_request(*, chat_request, tenant, session_id: str, user_message: str, source_page: str = "") -> dict:
    from assistant_core.consultative_policy import detect_collection_trigger, CollectionTrigger
    from assistant_core.dialogue_memory import (
        build_collection_slot_context,
        build_contextual_retrieval_query,
        load_dialogue_memory,
        persist_dialogue_memory,
        update_dialogue_memory_from_turn,
    )
    from assistant_core.services.response_quality_gate import apply_response_quality_gate

    decision_service = LiviaDecisionService()
    # Garante conversation_id em RagRetrievalEvent sem alterar a política de resposta.
    conversation_ref = _ensure_conversation_for_retrieval(tenant=tenant, session_id=session_id, source_page=source_page)
    history_preview = list(conversation_ref.messages.values("role", "content").order_by("created_at", "id"))
    memory = load_dialogue_memory(conversation_ref)
    discovery_preview = analyze_message(user_message)
    commercial = detect_collection_trigger(user_message) != CollectionTrigger.NONE
    slot_context = build_collection_slot_context(conversation=conversation_ref, message=user_message)
    memory = update_dialogue_memory_from_turn(
        memory=memory,
        current_message=user_message,
        history=history_preview,
        need_summary=memory.active_need,
        commercial_trigger=commercial,
        tenant=tenant,
        slot_context=slot_context,
    )
    original_query, contextual_query = build_contextual_retrieval_query(
        current_message=user_message,
        memory=memory,
        history=history_preview,
        need_summary=memory.active_need,
    )
    memory.retrieval_query_original = original_query
    memory.retrieval_query_contextual = contextual_query

    assistant_profile_preview = _active_assistant_profile(tenant)
    rag_limit = _rag_retrieval_limit(assistant_profile_preview)

    # Recuperação semântica pode chamar provider externo: permanece fora da transação de negócio.
    knowledge_result = build_knowledge_context_result(
        tenant,
        user_message,
        service_area=discovery_preview.service_area,
        limit=rag_limit,
        conversation=conversation_ref,
        contextual_query=contextual_query,
        active_domain=memory.active_domain,
        active_entity=memory.active_entity,
        active_application=memory.active_application,
        active_subject=memory.active_knowledge_subject,
        retrieval_query_original=original_query,
    )
    knowledge_context = knowledge_result.text
    memory.entity_match = bool(knowledge_result.entity_match or memory.entity_match)
    memory.domain_match = bool(knowledge_result.domain_match or memory.domain_match)

    deterministic_result = _persist_chat_processing_state(
        chat_request=chat_request,
        tenant=tenant,
        session_id=session_id,
        user_message=user_message,
        source_page=source_page,
        decision_service=decision_service,
        knowledge_context=knowledge_context,
        knowledge_result=knowledge_result,
        dialogue_memory=memory,
    )
    return _refine_response_with_ai_if_enabled(
        deterministic_result=deterministic_result,
        decision_service=decision_service,
        knowledge_context=knowledge_context,
    )


def _ensure_conversation_for_retrieval(*, tenant, session_id: str, source_page: str) -> Conversation:
    existing = Conversation.objects.filter(tenant=tenant, session_id=session_id).first()
    if existing is not None:
        return existing
    try:
        return Conversation.objects.create(tenant=tenant, session_id=session_id, source_page=source_page or "")
    except IntegrityError:
        return Conversation.objects.get(tenant=tenant, session_id=session_id)


def _persist_chat_processing_state(
    *,
    chat_request,
    tenant,
    session_id: str,
    user_message: str,
    source_page: str,
    decision_service: LiviaDecisionService,
    knowledge_context: str = "",
    knowledge_result: KnowledgeContextResult | None = None,
    dialogue_memory=None,
) -> _DeterministicChatResult:
    from assistant_core.dialogue_memory import persist_dialogue_memory, should_skip_consultative_followup
    from assistant_core.services.response_quality_gate import apply_response_quality_gate

    with transaction.atomic():
        conversation = _get_or_create_locked_conversation(tenant=tenant, session_id=session_id, source_page=source_page)
        assistant_profile = _active_assistant_profile(tenant)
        history = list(conversation.messages.values("role", "content").order_by("created_at", "id"))

        # Mantém decisão determinística no bloco atômico e adia IA externa para fora da transação.
        decision_started = time.monotonic()
        decision = decision_service.generate_reply(
            history=history,
            current_message=user_message,
            conversation=conversation,
            assistant_profile=_assistant_profile_without_ai(assistant_profile),
            knowledge_context=knowledge_context,
            dialogue_memory=dialogue_memory,
        )
        decision_ms = int((time.monotonic() - decision_started) * 1000)
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.USER,
            content=user_message,
        )

        assistant_reply = str(decision.reply or "").strip()
        from assistant_core.continuity_policy import OFFER
        continuity_action = getattr(decision, "continuity_action", "")
        if continuity_action == "offered":
            assistant_reply = assistant_reply.removesuffix(OFFER).rstrip()
        human_handoff_payload = None
        if decision.handoff_request_id and decision.handoff_reason == HandoffRequest.Reason.EXPLICIT_REQUEST:
            handoff = HandoffRequest.objects.filter(
                pk=decision.handoff_request_id,
                tenant=tenant,
                conversation=conversation,
            ).first()
            human_handoff_payload = build_human_handoff_payload(assistant_profile, handoff)
            if human_handoff_payload.get("active"):
                assistant_reply = "Claro. Use o botão do WhatsApp que apareceu na tela para falar com nossa equipe."

        gate_diagnostics = {}
        if dialogue_memory is not None and human_handoff_payload is None:
            try:
                lead = conversation.lead_draft
            except Exception:
                lead = None
            collection_active = bool(
                lead is not None
                and isinstance(getattr(lead, "qualification_data", None), dict)
                and (lead.qualification_data or {}).get("collection_active")
            )
            ai_primary = _will_use_openai_primary(assistant_profile)
            # Não reescrever prompts de coleta (nome/telefone) com síntese determinística.
            # Com OpenAI primária, a LLM gera linguagem após commit; gate determinístico fica como fallback.
            if not collection_active and not ai_primary and continuity_action != "declined":
                assistant_reply, gate_diagnostics = apply_response_quality_gate(
                    reply=assistant_reply,
                    knowledge_context=knowledge_context,
                    current_message=user_message,
                    memory=dialogue_memory,
                    need_summary=str(getattr(lead, "need_summary", "") or "") if lead is not None else "",
                    history=history,
                    append_followup=False if should_skip_consultative_followup(current_message=user_message, memory=dialogue_memory) else None,
                )
            if lead is not None:
                persist_dialogue_memory(lead, dialogue_memory)

        assistant_reply = str(assistant_reply or "").strip()
        if continuity_action == "offered":
            assistant_reply = f"{assistant_reply}\n\n{OFFER}"
        if not assistant_reply:
            # Fail-closed: sucesso HTTP nunca devolve reply vazia.
            assistant_reply = DEFAULT_REPLY
            logger.warning(
                "livia_empty_reply_replaced tenant_slug=%s session_id=%s",
                getattr(tenant, "slug", ""),
                session_id,
            )

        assistant_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content=assistant_reply,
        )

        observability = {
            # intent = classificação conversacional da mensagem (discovery).
            # commercial_intent (memória) = gatilho comercial persistido para coleta/handoff.
            "is_fallback": bool(is_generic_fallback_reply(assistant_reply)),
            "intent": getattr(decision, "intent", "") or "",
            "retrieval_attempted": bool(knowledge_result and knowledge_result.mode in {"semantic", "keyword"}),
            "retrieval_status": getattr(knowledge_result, "retrieval_status", "") or "",
            "retrieval_hit": bool(getattr(knowledge_result, "retrieval_hit", False)),
            "max_score": float(getattr(knowledge_result, "max_score", 0.0) or 0.0),
            "chunk_count": int(getattr(knowledge_result, "result_count", 0) or 0),
            "retrieval_ms": int(getattr(knowledge_result, "duration_ms", 0) or 0),
            "decision_ms": decision_ms,
            "retrieval_mode": getattr(knowledge_result, "mode", "") or "",
            "policy_chunks_filtered": int(getattr(knowledge_result, "policy_chunks_filtered", 0) or 0),
            "coherence_filtered_count": int(
                getattr(knowledge_result, "coherence_filtered_count", 0) or gate_diagnostics.get("coherence_filtered_count", 0) or 0
            ),
            "collection_trigger_reason": "",
            "followup_strategy": gate_diagnostics.get("followup_strategy", ""),
            "policy_leak_blocked": bool(gate_diagnostics.get("policy_leak_blocked")),
            "policy_chunk_selected": 0,
            "active_subject": getattr(dialogue_memory, "active_knowledge_subject", {}) if dialogue_memory is not None else {},
            "contextual_query_used": bool(
                dialogue_memory
                and dialogue_memory.retrieval_query_contextual
                and dialogue_memory.retrieval_query_contextual != dialogue_memory.retrieval_query_original
            ),
        }
        if dialogue_memory is not None:
            observability.update(dialogue_memory.observability())
            observability["entity_match"] = bool(dialogue_memory.entity_match or getattr(knowledge_result, "entity_match", False))
            observability["domain_match"] = bool(dialogue_memory.domain_match or getattr(knowledge_result, "domain_match", False))
            observability["contextual_query_used"] = bool(
                dialogue_memory.retrieval_query_contextual
                and dialogue_memory.retrieval_query_contextual.strip()
                and dialogue_memory.retrieval_query_contextual.strip()
                != (dialogue_memory.retrieval_query_original or "").strip()
            )
        # Motivo de coleta quando disponível no lead.
        try:
            lead_obs = conversation.lead_draft
            qd = dict(getattr(lead_obs, "qualification_data", None) or {})
            if qd.get("collection_active"):
                observability["collection_trigger_reason"] = qd.get("collection_trigger_reason") or "collection_active"
            elif qd.get("contact_collection_deferred"):
                observability["collection_trigger_reason"] = "contact_deferred"
        except Exception:
            pass
        # Observabilidade na resposta/API e no ChatRequest deve ser idêntica (idempotência).
        # Queries completas ficam no RagRetrievalEvent; aqui só flags e domínio/entidade.
        client_observability = {
            key: value
            for key, value in observability.items()
            if key not in {"retrieval_query_original", "retrieval_query_contextual"}
        }
        response_payload = {
            "tenant": tenant.slug,
            "session_id": session_id,
            "session_key": session_id,
            "reply": assistant_reply,
            "intent": decision.intent,
            "assistant_name": getattr(assistant_profile, "name", "Lívia"),
            "initial_message": getattr(
                assistant_profile,
                "initial_message",
                "Olá! Sou a Lívia. Como posso te ajudar?",
            ),
            "observability": client_observability,
        }
        if human_handoff_payload is not None:
            response_payload["human_handoff"] = human_handoff_payload
        complete_chat_request(chat_request, response_payload=response_payload, status_code=200, conversation=conversation)
        return _DeterministicChatResult(
            chat_request=chat_request,
            tenant=tenant,
            conversation=conversation,
            assistant_profile=assistant_profile,
            history=history,
            user_message=user_message,
            decision=decision,
            assistant_message=assistant_message,
            response_payload=response_payload,
            dialogue_memory=dialogue_memory,
            knowledge_result=knowledge_result,
        )


def _refine_response_with_ai_if_enabled(
    *,
    deterministic_result: _DeterministicChatResult,
    decision_service: LiviaDecisionService,
    knowledge_context: str = "",
) -> dict:
    if not _can_refine_with_ai(deterministic_result.assistant_profile):
        return deterministic_result.response_payload
    if getattr(deterministic_result.decision, "continuity_action", ""):
        return deterministic_result.response_payload
    if getattr(deterministic_result.decision, "collection_prompt", False):
        return deterministic_result.response_payload
    if deterministic_result.response_payload.get("human_handoff", {}).get("active"):
        return deterministic_result.response_payload

    from assistant_core.services.response_quality_gate import apply_response_quality_gate
    from assistant_core.services.openai_grounded_conversation import OpenAIGroundedConversationService

    discovery = analyze_message(deterministic_result.user_message)
    context = knowledge_context or build_knowledge_context(
        deterministic_result.tenant,
        deterministic_result.user_message,
        service_area=discovery.service_area,
        limit=_rag_retrieval_limit(deterministic_result.assistant_profile),
        conversation=deterministic_result.conversation,
    )

    conversation_service = OpenAIGroundedConversationService(ai_client=decision_service.ai_client)
    try:
        ai_result = conversation_service.generate(
            tenant=deterministic_result.tenant,
            assistant_profile=deterministic_result.assistant_profile,
            message=deterministic_result.user_message,
            conversation=deterministic_result.conversation,
            discovery=discovery,
            decision=deterministic_result.decision,
            knowledge_context=context,
            history=deterministic_result.history,
            dialogue_memory=deterministic_result.dialogue_memory,
            knowledge_result=deterministic_result.knowledge_result,
            deterministic_reply=str(deterministic_result.response_payload.get("reply", "") or ""),
        )
    except Exception:
        logger.exception(
            "ai.conversation.unhandled tenant_slug=%s session_hash_unavailable",
            deterministic_result.tenant.slug,
        )
        return _mark_ai_fallback_observability(deterministic_result.response_payload)

    if ai_result.used and ai_result.text:
        try:
            lead = deterministic_result.conversation.lead_draft
            need_summary = str(getattr(lead, "need_summary", "") or "") if lead is not None else ""
        except Exception:
            need_summary = ""
        reply, gate_diagnostics = apply_response_quality_gate(
            reply=ai_result.text,
            knowledge_context=context,
            current_message=deterministic_result.user_message,
            memory=deterministic_result.dialogue_memory,
            need_summary=need_summary,
            history=deterministic_result.history,
            llm_primary=True,
        )
        if not reply.strip():
            return _mark_ai_fallback_observability(deterministic_result.response_payload)
        from assistant_core.continuity_policy import offer_after_guidance

        handoff = bool(getattr(deterministic_result.decision, "handoff_request_id", None))
        final_reply, continuity_action = offer_after_guidance(
            conversation=deterministic_result.conversation,
            message=deterministic_result.user_message,
            history=deterministic_result.history,
            reply=reply,
            knowledge_context=context,
            handoff=handoff,
        )
        if continuity_action != "offered":
            final_reply = reply
        return _apply_refined_reply(
            deterministic_result,
            final_reply,
            ai_mode="openai_conversation",
            ai_observability={
                "ai_provider": "openai",
                "ai_model": ai_result.model,
                "ai_latency_ms": ai_result.latency_ms,
                "ai_prompt_tokens": ai_result.prompt_tokens,
                "ai_completion_tokens": ai_result.completion_tokens,
                "ai_total_tokens": ai_result.total_tokens,
                "ai_grounded": ai_result.grounded,
                "ai_fallback_used": False,
                "ai_rag_docs": ai_result.metadata.get("rag_docs", []),
                "continuity_action": continuity_action,
                **gate_diagnostics,
            },
        )

    # Fallback seguro: resposta determinística já persistida.
    logger.info(
        "ai.conversation.fallback tenant_slug=%s reason=%s",
        deterministic_result.tenant.slug,
        ai_result.skip_reason or ai_result.status,
    )
    return _mark_ai_fallback_observability(
        deterministic_result.response_payload,
        ai_observability={
            "ai_provider": "openai",
            "ai_model": ai_result.model,
            "ai_latency_ms": ai_result.latency_ms,
            "ai_fallback_used": True,
            "ai_error_type": ai_result.error_type,
            "ai_skip_reason": ai_result.skip_reason,
        },
    )


def _mark_ai_fallback_observability(response_payload: dict, ai_observability: dict | None = None) -> dict:
    updated = dict(response_payload)
    observability = dict(updated.get("observability") or {})
    observability.update(ai_observability or {"ai_fallback_used": True})
    updated["observability"] = observability
    return updated


def _apply_refined_reply(
    deterministic_result: _DeterministicChatResult,
    reply: str,
    *,
    ai_mode: str,
    ai_observability: dict | None = None,
) -> dict:
    updated_payload = dict(deterministic_result.response_payload)
    updated_payload["reply"] = reply
    updated_payload["ai_mode"] = ai_mode
    if ai_observability:
        observability = dict(updated_payload.get("observability") or {})
        observability.update(ai_observability)
        updated_payload["observability"] = observability
    with transaction.atomic():
        Message.objects.filter(pk=deterministic_result.assistant_message.pk).update(content=reply)
        update_completed_chat_request_response(
            deterministic_result.chat_request,
            response_payload=updated_payload,
            status_code=200,
        )
    return updated_payload


def _active_assistant_profile(tenant):
    try:
        assistant_profile = tenant.assistant_profile
    except ObjectDoesNotExist:
        return None
    if assistant_profile is not None and not assistant_profile.is_active:
        return None
    return assistant_profile


def _assistant_profile_without_ai(assistant_profile):
    if assistant_profile is None or not getattr(assistant_profile, "use_ai", False):
        return assistant_profile
    return SimpleNamespace(
        name=getattr(assistant_profile, "name", ""),
        initial_message=getattr(assistant_profile, "initial_message", ""),
        tone=getattr(assistant_profile, "tone", ""),
        primary_goal=getattr(assistant_profile, "primary_goal", ""),
        business_name=getattr(assistant_profile, "business_name", ""),
        business_domain=getattr(assistant_profile, "business_domain", ""),
        short_description=getattr(assistant_profile, "short_description", ""),
        use_ai=False,
    )


def _can_refine_with_ai(assistant_profile) -> bool:
    if assistant_profile is None:
        return False
    if bool(getattr(settings, "RUNNING_TESTS", False)):
        return False
    from assistant_core.services.ai_feature_gates import is_openai_conversation_allowed

    return is_openai_conversation_allowed(assistant_profile=assistant_profile)


def _will_use_openai_primary(assistant_profile) -> bool:
    """Indica se a resposta final virá da OpenAI (fora da transação)."""
    return _can_refine_with_ai(assistant_profile)


def _rag_retrieval_limit(assistant_profile) -> int:
    return 4 if _will_use_openai_primary(assistant_profile) else 2


def _get_or_create_locked_conversation(*, tenant, session_id: str, source_page: str = "") -> Conversation:
    queryset = Conversation.objects.filter(tenant=tenant, session_id=session_id)
    if connection.features.has_select_for_update:
        queryset = queryset.select_for_update()
    conversation = queryset.first()
    if conversation is None:
        try:
            conversation = Conversation.objects.create(
                tenant=tenant,
                session_id=session_id,
                source_page=source_page,
            )
        except IntegrityError:
            queryset = Conversation.objects.filter(tenant=tenant, session_id=session_id)
            if connection.features.has_select_for_update:
                queryset = queryset.select_for_update()
            conversation = queryset.get()
    elif source_page and not conversation.source_page:
        conversation.source_page = source_page
        conversation.save(update_fields=["source_page", "updated_at"])
    return conversation
