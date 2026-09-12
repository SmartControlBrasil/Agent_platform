from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Iterable

from django.conf import settings

from assistant_core.conversation_turns import (
    TurnKind,
    build_direct_question_reply,
    build_name_deferred_reply,
    classify_conversation_turn,
    mark_name_deferred,
    normalize_text,
)
from assistant_core.consultative_policy import (
    CollectionTrigger,
    build_consultative_commercial_reply,
    build_conceptual_price_reply,
    decide_collection,
    is_conceptual_price_question,
    is_consultative_need_discovery,
    mark_collection_active,
)
from assistant_core.discovery import analyze_message
from assistant_core.discovery.contextual import resolve_discovery_question, should_ask_profile_discovery
from assistant_core.services.decision_outcome import has_semantic_knowledge_block, should_combine_kb_with_discovery, is_informational_knowledge_query
from assistant_core.prompts import (
    DEFAULT_REPLY,
    build_contextual_reply,
)
from assistant_core.services.deterministic_synthesis import (
    is_generic_fallback_reply,
    prefer_contextual_reply_over_fallback,
    synthesize_deterministic_reply,
)
from assistant_core.prompts.livia_ai import build_livia_ai_prompt
from assistant_core.qualification import has_basic_contact
from assistant_core.state import LeadState, can_start_new_cycle, set_state, should_block_dialogue_for_locked_lead
from assistant_core.services.decision_outcome import is_consultative_knowledge_turn
from leads.services import CRMDispatchService, LeadCaptureService
from leads.services.handoff import HandoffService
from integrations.openai.client import OpenAIChatClient
from knowledge_base.rag.context_builder import build_knowledge_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiviaReply:
    intent: str
    reply: str
    handoff_request_id: int | None = None
    handoff_reason: str = ""
    continuity_action: str = ""
    collection_prompt: bool = False


@dataclass(frozen=True)
class AssistantProfileContext:
    name: str = "Lívia"
    initial_message: str = "Olá! Sou a Lívia. Como posso te ajudar?"
    tone: str = "consultivo, claro e profissional"
    primary_goal: str = "qualificar leads"
    business_name: str = ""
    business_domain: str = ""
    short_description: str = ""

    @classmethod
    def from_profile(cls, profile, tenant=None) -> "AssistantProfileContext":
        if profile is None:
            return cls()
        tenant_name = str(getattr(tenant, "name", "") or "").strip()
        business_name = str(getattr(profile, "business_name", "") or "").strip() or tenant_name
        return cls(
            name=str(getattr(profile, "name", "") or "Lívia").strip() or "Lívia",
            initial_message=str(
                getattr(profile, "initial_message", "")
                or "Olá! Sou a Lívia. Como posso te ajudar?"
            ).strip(),
            tone=str(getattr(profile, "tone", "") or cls.tone).strip(),
            primary_goal=str(getattr(profile, "primary_goal", "") or cls.primary_goal).strip(),
            business_name=business_name,
            business_domain=str(getattr(profile, "business_domain", "") or "").strip(),
            short_description=str(getattr(profile, "short_description", "") or "").strip(),
        )


class LiviaDecisionService:
    def __init__(
        self,
        lead_capture_service: LeadCaptureService | None = None,
        crm_dispatch_service: CRMDispatchService | None = None,
        handoff_service: HandoffService | None = None,
        ai_client: OpenAIChatClient | None = None,
    ):
        self.lead_capture_service = lead_capture_service or LeadCaptureService()
        self.crm_dispatch_service = crm_dispatch_service or CRMDispatchService()
        self.handoff_service = handoff_service or HandoffService()
        self.ai_client = ai_client or OpenAIChatClient()

    def generate_reply(
        self,
        history: Iterable[dict[str, str]],
        current_message: str,
        conversation=None,
        assistant_profile=None,
        knowledge_context: str | None = None,
        dialogue_memory=None,
    ) -> LiviaReply:
        tenant = getattr(conversation, "tenant", None)
        profile_context = AssistantProfileContext.from_profile(assistant_profile, tenant=tenant)
        discovery = analyze_message(current_message)
        classification = discovery.to_dict()
        intent = discovery.intent
        if knowledge_context is None:
            knowledge_context = build_knowledge_context(
                tenant,
                current_message,
                service_area=discovery.service_area,
                limit=2,
                conversation=conversation,
            ) if tenant is not None else ""
        else:
            knowledge_context = str(knowledge_context or "")
        if conversation is not None:
            from assistant_core.relational_collection import capture_passive_fields
            from leads.services.commercial import resolve_lead_draft

            passive_lead = resolve_lead_draft(conversation)
            if passive_lead is not None and not (passive_lead.qualification_data or {}).get("collection_active"):
                capture_passive_fields(lead=passive_lead, message=current_message, history=history)
            from assistant_core.continuity_policy import resolve_offer_response, DECLINE_REPLY
            from assistant_core.consultative_policy import is_explicit_collection_trigger

            offer_response = resolve_offer_response(
                conversation=conversation, message=current_message, history=history,
            )
            if offer_response == "declined":
                return LiviaReply(intent=intent, reply=DECLINE_REPLY, continuity_action="declined")
            if offer_response == "accepted" and not is_explicit_collection_trigger(current_message):
                decision = self._handle_qualification(
                    intent="commercial_interest", history=history, current_message="",
                    conversation=conversation, discovery=discovery, assistant_profile=assistant_profile,
                    knowledge_context=knowledge_context, activate_collection=True,
                    collection_reason="continuity_offer_accepted",
                )
                return replace(decision, continuity_action="accepted")
        has_commercial_interest = bool(discovery.has_commercial_interest)
        has_quote_request = bool(discovery.has_quote_request)
        has_support_request = bool(discovery.has_support_request)
        has_technical_question = bool(discovery.has_technical_question)
        if conversation is not None and should_block_dialogue_for_locked_lead(conversation, current_message, discovery):
            decision = self._locked_lead_reply(intent if intent not in {"unknown", "greeting"} else "commercial_interest")
            return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
        turn = classify_conversation_turn(
            current_message=current_message,
            history=history,
            conversation=conversation,
            discovery=discovery,
        )
        collection_gate = decide_collection(
            current_message=current_message,
            conversation=conversation,
            discovery=discovery,
        )
        if conversation is not None and collection_gate.should_collect and turn.kind == TurnKind.OTHER:
            return self._handle_qualification(
                intent=discovery.intent if discovery.intent not in {"unknown", "greeting"} else "commercial_interest",
                history=history,
                current_message=current_message,
                conversation=conversation,
                discovery=discovery,
                assistant_profile=assistant_profile,
                knowledge_context=knowledge_context,
                activate_collection=True,
                collection_reason=getattr(collection_gate, "reason", "") or "",
            )
        if conversation is not None and turn.kind != TurnKind.OTHER:
            return self._handle_contextual_turn(
                turn=turn,
                intent=discovery.intent if discovery.intent not in {"unknown", "greeting", "contact_data"} else "commercial_interest",
                history=history,
                current_message=current_message,
                conversation=conversation,
                discovery=discovery,
                assistant_profile=assistant_profile,
                knowledge_context=knowledge_context,
                dialogue_memory=dialogue_memory,
            )
        from assistant_core.consultative_policy import collection_already_active
        from assistant_core.services.decision_outcome import is_consultative_knowledge_message

        if (
            conversation is not None
            and collection_already_active(conversation)
            and not collection_gate.should_collect
            and (
                is_consultative_knowledge_message(current_message)
                or is_consultative_knowledge_turn(discovery, current_message)
            )
        ):
            return self._handle_consultative_conversation(
                intent=discovery.intent if discovery.intent not in {"unknown", "greeting"} else "commercial_interest",
                history=history,
                current_message=current_message,
                conversation=conversation,
                discovery=discovery,
                assistant_profile=assistant_profile,
                knowledge_context=knowledge_context,
                dialogue_memory=dialogue_memory,
            )

        if intent == "greeting":
            decision = LiviaReply(intent=intent, reply=profile_context.initial_message)
            return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
        if intent == "technical_question":
            if self._should_answer_informatively_from_knowledge(discovery, knowledge_context):
                reply = build_contextual_reply(intent="technical_question")
                decision = LiviaReply(intent=intent, reply=self._with_knowledge(reply, knowledge_context))
                decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
                return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
            if discovery.should_ask_discovery_question and discovery.suggested_next_question:
                decision = self._ask_discovery_question(
                    discovery, conversation, knowledge_context=knowledge_context,
                    assistant_profile=assistant_profile, tenant=tenant,
                )
                decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
                return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
            reply = build_contextual_reply(intent="technical_question")
            decision = LiviaReply(intent=intent, reply=self._with_knowledge(reply, knowledge_context))
            decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
            return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
        if intent == "support_request":
            decision = LiviaReply(
                intent=intent,
                reply=self._with_knowledge(build_contextual_reply(intent="support_request"), knowledge_context),
            )
            decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
            return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
        if intent in {"quote_request", "commercial_interest"}:
            if conversation is not None and should_block_dialogue_for_locked_lead(conversation, current_message, discovery):
                decision = self._locked_lead_reply(intent)
                return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
            collection = decide_collection(
                current_message=current_message,
                conversation=conversation,
                discovery=discovery,
            )
            if not collection.should_collect:
                if self._should_answer_informatively_from_knowledge(discovery, knowledge_context):
                    decision = self._handle_consultative_conversation(
                        intent=intent,
                        history=history,
                        current_message=current_message,
                        conversation=conversation,
                        discovery=discovery,
                        assistant_profile=assistant_profile,
                        knowledge_context=knowledge_context,
                        dialogue_memory=dialogue_memory,
                    )
                    return decision
                if self._should_ask_profile_discovery(current_message, assistant_profile):
                    decision = self._ask_discovery_question(
                        discovery, conversation, knowledge_context=knowledge_context,
                        assistant_profile=assistant_profile, tenant=tenant, current_message=current_message,
                    )
                    decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
                    return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
                if should_combine_kb_with_discovery(discovery, conversation, knowledge_context):
                    decision = self._ask_discovery_question(
                        discovery, conversation, knowledge_context=knowledge_context,
                        assistant_profile=assistant_profile, tenant=tenant, current_message=current_message,
                    )
                    decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
                    return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
                if not discovery.should_collect_lead and discovery.should_ask_discovery_question and discovery.suggested_next_question:
                    # Prefer discovery when message is still vague and no profile-specific shape exists.
                    if len(str(current_message or "").split()) <= 4 and not discovery.should_answer_contextually:
                        decision = self._ask_discovery_question(
                            discovery, conversation, knowledge_context=knowledge_context,
                            assistant_profile=assistant_profile, tenant=tenant, current_message=current_message,
                        )
                        decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
                        return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
                decision = self._handle_consultative_conversation(
                    intent=intent,
                    history=history,
                    current_message=current_message,
                    conversation=conversation,
                    discovery=discovery,
                    assistant_profile=assistant_profile,
                    knowledge_context=knowledge_context,
                    dialogue_memory=dialogue_memory,
                )
                return decision
            if self._should_ask_profile_discovery(current_message, assistant_profile) and collection.trigger == CollectionTrigger.NONE:
                decision = self._ask_discovery_question(
                    discovery, conversation, knowledge_context=knowledge_context,
                    assistant_profile=assistant_profile, tenant=tenant, current_message=current_message,
                )
                decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
                return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
            if should_combine_kb_with_discovery(discovery, conversation, knowledge_context) and not collection.should_collect:
                decision = self._ask_discovery_question(
                    discovery, conversation, knowledge_context=knowledge_context,
                    assistant_profile=assistant_profile, tenant=tenant, current_message=current_message,
                )
                decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
                return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
            if self._should_answer_informatively_from_knowledge(discovery, knowledge_context) and not collection.should_collect:
                decision = LiviaReply(
                    intent=intent,
                    reply=self._with_knowledge(
                        build_conceptual_price_reply(current_message=current_message)
                        if is_conceptual_price_question(current_message)
                        else "",
                        knowledge_context,
                    ) or prefer_contextual_reply_over_fallback(
                        knowledge_context=knowledge_context or "",
                        current_message=current_message,
                        history=history,
                    ),
                )
                decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
                return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
            return self._handle_qualification(
                intent=intent,
                history=history,
                current_message=current_message,
                conversation=conversation,
                discovery=discovery,
                assistant_profile=assistant_profile,
                knowledge_context=knowledge_context,
                activate_collection=True,
                collection_reason=getattr(collection, "reason", "") or "",
            )
        if intent == "contact_data":
            collection = decide_collection(
                current_message=current_message,
                conversation=conversation,
                discovery=discovery,
            )
            if collection.should_collect:
                return self._handle_qualification(
                    intent="contact_data",
                    history=history,
                    current_message=current_message,
                    conversation=conversation,
                    discovery=discovery,
                    assistant_profile=assistant_profile,
                    knowledge_context=knowledge_context,
                    activate_collection=True,
                    collection_reason=getattr(collection, "reason", "") or "",
                )
            if has_basic_contact(current_message) and (has_quote_request or has_commercial_interest):
                # Contact alone during consultative mode keeps conversation going,
                # but if the visitor already provided contact + commercial context
                # after an active collection, qualification handles it above.
                decision = self._handle_consultative_conversation(
                    intent="commercial_interest",
                    history=history,
                    current_message=current_message,
                    conversation=conversation,
                    discovery=discovery,
                    assistant_profile=assistant_profile,
                    knowledge_context=knowledge_context,
                    dialogue_memory=dialogue_memory,
                )
                return decision
            decision = LiviaReply(
                intent=intent,
                reply=build_contextual_reply(intent="contact_data"),
            )
            decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
            return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
        if has_basic_contact(current_message) and (has_commercial_interest or has_quote_request):
            if conversation is not None and should_block_dialogue_for_locked_lead(conversation, current_message, discovery):
                decision = self._locked_lead_reply("contact_data")
                return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
            collection = decide_collection(
                current_message=current_message,
                conversation=conversation,
                discovery=discovery,
            )
            if not collection.should_collect:
                return self._handle_consultative_conversation(
                    intent="commercial_interest",
                    history=history,
                    current_message=current_message,
                    conversation=conversation,
                    discovery=discovery,
                    assistant_profile=assistant_profile,
                    knowledge_context=knowledge_context,
                    dialogue_memory=dialogue_memory,
                )
            return self._handle_qualification(
                intent="contact_data",
                history=history,
                current_message=current_message,
                conversation=conversation,
                discovery=discovery,
                assistant_profile=assistant_profile,
                knowledge_context=knowledge_context,
                activate_collection=True,
                collection_reason=getattr(collection, "reason", "") or "",
            )
        if has_support_request or has_technical_question:
            decision = LiviaReply(
                intent=intent,
                reply=self._with_knowledge(build_contextual_reply(intent=intent), knowledge_context),
            )
            decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
            return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
        if self._is_followup_from_history(history):
            need = ""
            if conversation is not None:
                try:
                    need = str(getattr(conversation.lead_draft, "need_summary", "") or "")
                except Exception:
                    need = ""
            reply = prefer_contextual_reply_over_fallback(
                knowledge_context=knowledge_context or "",
                need_summary=need,
                current_message=current_message,
                history=history,
            )
            decision = LiviaReply(
                intent="followup",
                reply=reply,
            )
            decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
            return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
        if discovery.should_ask_discovery_question and has_semantic_knowledge_block(knowledge_context) and not is_informational_knowledge_query(discovery):
            decision = self._ask_discovery_question(
                discovery,
                conversation,
                knowledge_context=knowledge_context,
                assistant_profile=assistant_profile,
                tenant=tenant,
            )
            decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
            return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
        if is_consultative_knowledge_turn(discovery, current_message):
            decision = self._handle_consultative_conversation(
                intent=intent if intent not in {"unknown", "greeting"} else "commercial_interest",
                history=history,
                current_message=current_message,
                conversation=conversation,
                discovery=discovery,
                assistant_profile=assistant_profile,
                knowledge_context=knowledge_context,
                dialogue_memory=dialogue_memory,
            )
            return decision
        need = ""
        if conversation is not None:
            try:
                need = str(getattr(conversation.lead_draft, "need_summary", "") or "")
            except Exception:
                need = ""
        decision = LiviaReply(
            intent=intent,
            reply=prefer_contextual_reply_over_fallback(
                knowledge_context=knowledge_context or "",
                need_summary=need,
                current_message=current_message,
                history=history,
            ),
        )
        decision = self._finalize_handoff(decision, conversation, None, discovery, current_message)
        return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)


    def _handle_contextual_turn(
        self,
        *,
        turn,
        intent: str,
        history,
        current_message: str,
        conversation,
        discovery,
        assistant_profile,
        knowledge_context: str,
        dialogue_memory=None,
    ) -> LiviaReply:
        result = None
        if conversation is not None:
            result = self.lead_capture_service.capture_from_message(
                conversation=conversation,
                message=current_message,
                history=history,
            )
        if turn.kind == TurnKind.NAME_DEFERRED:
            mark_name_deferred(result.lead_draft)
            from assistant_core.consultative_policy import pause_collection

            pause_collection(result.lead_draft, deferred_contact=True)
        lead_draft = None if result is None else result.lead_draft
        if turn.kind == TurnKind.NAME_DEFERRED:
            # Prefer grounded knowledge when available (e.g. continue answering about Duno).
            grounded = self._with_knowledge(
                "",
                knowledge_context,
                conversation=conversation,
                lead_draft=lead_draft,
                current_message=current_message,
            )
            if grounded and not is_generic_fallback_reply(grounded):
                from assistant_core.services.response_quality_gate import apply_response_quality_gate
                from assistant_core.dialogue_memory import load_dialogue_memory

                memory = load_dialogue_memory(conversation, lead_draft)
                reply, _ = apply_response_quality_gate(
                    reply=grounded,
                    knowledge_context=knowledge_context,
                    current_message=current_message,
                    memory=memory,
                    append_followup=False,
                )
                reply = f"Tudo bem — seguimos só com as dúvidas por enquanto. {reply}".strip()
            else:
                reply = build_name_deferred_reply(lead_draft)
        elif turn.kind == TurnKind.DIRECT_QUESTION:
            if turn.question_type:
                reply = build_direct_question_reply(
                    lead_draft,
                    question_type=turn.question_type,
                    current_message=current_message,
                )
                reply = self._with_knowledge(
                    reply,
                    knowledge_context,
                    conversation=conversation,
                    lead_draft=lead_draft,
                    current_message=current_message,
                )
            else:
                reply = self._build_grounded_consultative_reply(
                    lead_draft=lead_draft,
                    conversation=conversation,
                    current_message=current_message,
                    history=history,
                    knowledge_context=knowledge_context,
                    enrichment_snippet=turn.enrichment_snippet,
                    append_followup=False,
                    dialogue_memory=dialogue_memory,
                )
        elif turn.kind == TurnKind.NEED_ENRICHMENT and not has_semantic_knowledge_block(knowledge_context):
            from assistant_core.consultative_policy import build_consultative_commercial_reply
            from assistant_core.conversation_turns import build_enrichment_reply

            if turn.enrichment_snippet:
                reply = build_enrichment_reply(
                    lead_draft,
                    snippet=turn.enrichment_snippet,
                    current_message=current_message,
                    history=history,
                )
            else:
                reply = build_consultative_commercial_reply(
                    lead_draft=lead_draft,
                    current_message=current_message,
                    history=history,
                )
        else:
            reply = self._build_grounded_consultative_reply(
                lead_draft=lead_draft,
                conversation=conversation,
                current_message=current_message,
                history=history,
                knowledge_context=knowledge_context,
                enrichment_snippet=turn.enrichment_snippet,
                append_followup=turn.kind == TurnKind.NEED_ENRICHMENT,
                dialogue_memory=dialogue_memory,
            )
        decision = LiviaReply(intent=intent, reply=reply)
        decision = self._finalize_handoff(decision, conversation, lead_draft, discovery, current_message)
        return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)

    def _finalize_ai_response(
        self,
        decision: LiviaReply,
        conversation,
        assistant_profile,
        discovery,
        current_message: str,
        history: Iterable[dict[str, str]],
        knowledge_context: str,
    ) -> LiviaReply:
        if not self._inline_openai_in_generate_reply_enabled(assistant_profile):
            from assistant_core.continuity_policy import offer_after_guidance
            from assistant_core.relational_collection import (
                build_relational_name_prompt,
                mark_relational_name_asked,
                should_offer_relational_name,
            )
            from leads.services.commercial import resolve_lead_draft

            reply, action = offer_after_guidance(
                conversation=conversation, message=current_message, history=history,
                reply=decision.reply, knowledge_context=knowledge_context,
                handoff=bool(decision.handoff_request_id),
            )
            lead = resolve_lead_draft(conversation)
            if (
                lead is not None
                and not decision.collection_prompt
                and not action
                and should_offer_relational_name(
                    conversation=conversation,
                    lead=lead,
                    history=history,
                    message=current_message,
                    reply=reply,
                    knowledge_context=knowledge_context,
                    handoff=bool(decision.handoff_request_id),
                )
            ):
                name_prompt = build_relational_name_prompt()
                if name_prompt.lower() not in str(reply or "").lower():
                    reply = f"{reply.rstrip()} {name_prompt}".strip()
                    mark_relational_name_asked(lead)
            return replace(decision, reply=reply, continuity_action=action)
        tenant = getattr(conversation, "tenant", None)
        lead_state = str(getattr(conversation, "lead_state", "") or "")
        try:
            messages = build_livia_ai_prompt(
                tenant=tenant,
                assistant_profile=assistant_profile,
                message=current_message,
                conversation=conversation,
                discovery_result=discovery,
                lead_state=lead_state,
                knowledge_context=knowledge_context,
                deterministic_reply=decision.reply,
                history=list(history or []),
            )
            result = self.ai_client.create_chat_completion(messages=messages)
        except Exception as exc:  # pragma: no cover - defensive local guard
            logger.warning("livia_ai_finalize_failed error_type=%s", exc.__class__.__name__)
            return decision
        if result.success and result.text:
            return replace(decision, reply=result.text)
        return decision

    def _inline_openai_in_generate_reply_enabled(self, assistant_profile) -> bool:
        """OpenAI inline em generate_reply permanece desligada de propósito.

        A síntese conversacional ocorre pós-commit em chat_processing via
        OpenAIGroundedConversationService, depois que estado comercial e
        persistência determinísticos já foram resolvidos.
        """
        return False

    # Alias legado — callers internos usam o nome explícito acima.
    _should_try_ai = _inline_openai_in_generate_reply_enabled


    def _finalize_handoff(self, decision: LiviaReply, conversation, lead_draft, discovery, current_message: str) -> LiviaReply:
        from assistant_core.consultative_policy import is_explicit_human_handoff
        from assistant_core.dialogue_memory import is_contact_deferred, wants_consultative_continue
        from leads.services.commercial import is_ready_for_commercial_notification

        if is_contact_deferred(current_message) or wants_consultative_continue(current_message):
            return decision
        result = self.handoff_service.create_or_update_handoff(
            conversation,
            lead_draft=lead_draft,
            discovery_result=discovery,
            message=current_message,
        )
        if result.handoff is None:
            return decision
        decision = replace(
            decision,
            handoff_request_id=result.handoff.pk,
            handoff_reason=result.handoff.reason,
        )
        if not result.created:
            return decision
        if is_ready_for_commercial_notification(lead_draft):
            return decision
        confirmation = self._handoff_confirmation(result.handoff, current_message=current_message)
        if is_explicit_human_handoff(current_message):
            return replace(decision, reply=confirmation)
        if confirmation.lower() in decision.reply.lower():
            return decision
        return replace(decision, reply=f"{confirmation}\n\n{decision.reply}")

    def _handoff_confirmation(self, handoff, *, current_message: str = "") -> str:
        from assistant_core.consultative_policy import is_explicit_human_handoff

        has_contact = bool(handoff.visitor_phone or handoff.visitor_email)
        name = handoff.visitor_name or handoff.visitor_company
        if has_contact:
            if name:
                return f"Perfeito, {name}. Registrei o pedido de contato para a equipe retornar com o contexto da conversa."
            return "Perfeito. Registrei o pedido de contato para a equipe retornar com o contexto da conversa."
        if is_explicit_human_handoff(current_message):
            return "Claro. Qual telefone ou e-mail prefere usar para o contato?"
        return "Claro. Vou registrar seu pedido para atendimento humano. Para agilizar, me informe seu nome e um telefone ou e-mail de contato."

    def _locked_lead_reply(self, intent: str) -> LiviaReply:
        return LiviaReply(
            intent=intent,
            reply="Perfeito, já encaminhei seus dados para sequência do atendimento. Se for uma nova demanda, me diga que é um novo pedido.",
        )

    def _ask_discovery_question(self, discovery, conversation, knowledge_context: str = "", assistant_profile=None, tenant=None, current_message: str = "") -> LiviaReply:
        if conversation is not None:
            next_state = LeadState.COLLECT_NEED if discovery.intent in {"quote_request", "commercial_interest"} else LeadState.DISCOVERY
            set_state(conversation, next_state)
        profile_ctx = AssistantProfileContext.from_profile(assistant_profile, tenant=tenant or getattr(conversation, "tenant", None))
        if profile_ctx.business_domain:
            reply = resolve_discovery_question(
                discovery.service_area,
                business_domain=profile_ctx.business_domain,
                business_name=profile_ctx.business_name,
                short_description=profile_ctx.short_description,
                primary_goal=profile_ctx.primary_goal,
                current_message=current_message,
                knowledge_context=knowledge_context,
            )
        elif discovery.suggested_next_question:
            reply = discovery.suggested_next_question
        else:
            reply = resolve_discovery_question(
                discovery.service_area,
                business_domain=profile_ctx.business_domain,
                business_name=profile_ctx.business_name,
                short_description=profile_ctx.short_description,
                primary_goal=profile_ctx.primary_goal,
                current_message=current_message,
                knowledge_context=knowledge_context,
            )
        return LiviaReply(intent=discovery.intent, reply=self._with_knowledge(reply, knowledge_context))


    def _should_ask_profile_discovery(self, current_message: str, assistant_profile) -> bool:
        if assistant_profile is None:
            return False
        return should_ask_profile_discovery(
            current_message=current_message,
            business_domain=getattr(assistant_profile, "business_domain", ""),
            short_description=getattr(assistant_profile, "short_description", ""),
            primary_goal=getattr(assistant_profile, "primary_goal", ""),
        )

    def _with_knowledge(
        self,
        reply: str,
        knowledge_context: str,
        *,
        conversation=None,
        lead_draft=None,
        current_message: str = "",
        dialogue_memory=None,
    ) -> str:
        """
        Fundamenta a reply determinística com fatos curados do RAG.

        Com LIVIA_AI_ENABLED=False, sintetiza 1-2 frases naturais a partir dos
        chunks — sem dump de markdown, score, fonte ou metadados internos.
        """
        memory = self._resolve_memory(conversation, lead_draft, dialogue_memory)
        return synthesize_deterministic_reply(
            knowledge_context,
            base_reply=reply,
            current_message=current_message,
            active_domain=memory.active_domain,
            active_application=getattr(memory, "active_application", "") or "",
        )

    def _resolve_memory(self, conversation, lead_draft, dialogue_memory=None):
        from assistant_core.dialogue_memory import load_dialogue_memory

        if dialogue_memory is not None:
            return dialogue_memory
        return load_dialogue_memory(conversation, lead_draft)

    def _consultative_synthesis_query(self, current_message: str, need_summary: str, memory) -> str:
        from assistant_core.services.response_quality_gate import _consultative_synthesis_query

        return _consultative_synthesis_query(current_message, need_summary, memory)

    def _build_grounded_consultative_reply(
        self,
        *,
        lead_draft,
        conversation,
        current_message: str,
        history,
        knowledge_context: str,
        enrichment_snippet: str = "",
        append_followup: bool = True,
        dialogue_memory=None,
    ) -> str:
        from assistant_core.consultative_slots import extract_consultative_slots, is_cleaning_consultation, select_cleaning_followup
        from assistant_core.conversation_turns import looks_like_environment_answer
        from assistant_core.dialogue_memory import should_skip_consultative_followup
        from assistant_core.followup_strategy import select_followup
        from assistant_core.services.decision_outcome import is_consultative_knowledge_message
        from assistant_core.services.response_quality_gate import (
            apply_response_quality_gate,
            has_substantive_consultative_content,
            is_acknowledgement_only_reply,
        )

        memory = self._resolve_memory(conversation, lead_draft, dialogue_memory)
        need = str(getattr(lead_draft, "need_summary", "") or "").strip()
        synthesis_query = self._consultative_synthesis_query(current_message, need, memory)
        skip_followup = (
            not append_followup
            or is_consultative_knowledge_message(current_message)
            or should_skip_consultative_followup(current_message=current_message, memory=memory)
        )
        grounded = synthesize_deterministic_reply(
            knowledge_context,
            base_reply="",
            current_message=synthesis_query,
            active_domain=memory.active_domain,
            active_application=getattr(memory, "active_application", "") or "",
        )
        if not has_substantive_consultative_content(grounded) and knowledge_context.strip():
            grounded = synthesize_deterministic_reply(
                knowledge_context,
                base_reply="",
                current_message=need or synthesis_query,
                active_domain=memory.active_domain,
                active_application=getattr(memory, "active_application", "") or "",
            )
        if not has_substantive_consultative_content(grounded):
            grounded = self._insufficient_evidence_reply(current_message, knowledge_context, memory=memory)

        ack = ""
        if enrichment_snippet or looks_like_environment_answer(current_message):
            ack = "Entendi, isso ajuda a detalhar a necessidade."

        follow = ""
        if not skip_followup:
            follow, _ = select_followup(
                memory=memory,
                current_message=current_message,
                need_summary=need,
                answer_text=grounded or "",
                history=history,
                force=False,
            )
            if not follow and is_cleaning_consultation(memory=memory, need_summary=need, current_message=current_message):
                slots = extract_consultative_slots(
                    need_summary=need,
                    history=history,
                    current_message=current_message,
                )
                follow = select_cleaning_followup(slots=slots, current_message=current_message)

        parts: list[str] = []
        if has_substantive_consultative_content(grounded):
            parts.append(grounded)
        if follow and follow.lower() not in normalize_text(" ".join(parts)):
            parts.append(follow)
        if ack and parts:
            parts.insert(1 if has_substantive_consultative_content(grounded) else 0, ack)

        reply = " ".join(parts).strip()
        if is_acknowledgement_only_reply(reply) and knowledge_context.strip():
            reply = " ".join(part for part in (grounded, follow) if part and str(part).strip()).strip()

        reply, _ = apply_response_quality_gate(
            reply=reply,
            knowledge_context=knowledge_context,
            current_message=current_message,
            memory=memory,
            need_summary=need,
            history=history,
            append_followup=False,
        )
        if not str(reply or "").strip() or is_acknowledgement_only_reply(reply) or is_generic_fallback_reply(reply):
            from assistant_core.consultative_policy import build_consultative_commercial_reply
            from assistant_core.conversation_turns import build_enrichment_reply

            if enrichment_snippet or looks_like_environment_answer(current_message):
                reply = build_enrichment_reply(
                    lead_draft,
                    snippet=enrichment_snippet,
                    current_message=current_message,
                    history=history,
                )
            else:
                reply = build_consultative_commercial_reply(
                    lead_draft=lead_draft,
                    current_message=current_message,
                    history=history,
                )
        return reply

    def _insufficient_evidence_reply(self, current_message: str, knowledge_context: str, *, memory=None) -> str:
        from assistant_core.services.deterministic_synthesis import _unsupported_requirement_reply

        unsupported = _unsupported_requirement_reply(knowledge_context, current_message=current_message)
        if unsupported:
            return unsupported
        msg = normalize_text(current_message)
        if "?" in str(current_message or "") and any(
            token in msg for token in ("circulando", "pessoas", "consegue", "funciona", "trabalha")
        ):
            product_hint = str(getattr(memory, "active_entity", "") or "").strip()
            subject = f" sobre {product_hint}" if product_hint else ""
            return (
                f"Não encontrei confirmação suficiente na documentação disponível{subject} "
                "para responder isso com segurança. Posso ajudar com outros detalhes técnicos do ambiente ou da operação."
            )
        return ""

    def _synthesize_knowledge_reply(self, knowledge_context: str) -> str:
        """Compat: delega para a camada determinística compartilhada."""
        return synthesize_deterministic_reply(knowledge_context, base_reply="")

    def _knowledge_hints(self, knowledge_context: str) -> str:
        """Compat: extrai texto sintetizado já limpo."""
        return synthesize_deterministic_reply(knowledge_context, base_reply="")

    def _is_followup_from_history(self, history: Iterable[dict[str, str]]) -> bool:
        messages = list(history or [])
        if not messages:
            return False
        last_user = next((message for message in reversed(messages) if message.get("role") == "user"), None)
        if not last_user:
            return False
        return len(str(last_user.get("content") or "").strip()) <= 18

    def _handle_consultative_conversation(
        self,
        *,
        intent: str,
        history,
        current_message: str,
        conversation,
        discovery,
        assistant_profile,
        knowledge_context: str,
        dialogue_memory=None,
    ) -> LiviaReply:
        from assistant_core.services.decision_outcome import (
            _is_product_information_discovery,
            is_consultative_knowledge_message,
            is_consultative_knowledge_turn,
        )
        from assistant_core.consultative_policy import collection_already_active
        from leads.services.commercial import resolve_lead_draft

        lead_draft = None
        locked_consultative_need = bool(
            conversation is not None
            and (getattr(conversation, "is_qualified", False) or getattr(conversation, "lead_state", "") == LeadState.QUALIFIED)
            and is_consultative_need_discovery(discovery, current_message)
        )
        if conversation is not None:
            lead_draft = resolve_lead_draft(conversation, lead_draft)
        knowledge_during_collection = bool(
            conversation is not None
            and collection_already_active(conversation, lead_draft)
            and is_consultative_knowledge_turn(discovery, current_message)
        )
        pure_consultative = knowledge_during_collection or (
            (is_consultative_knowledge_message(current_message) or _is_product_information_discovery(discovery) or locked_consultative_need)
            and not bool(getattr(discovery, "should_collect_lead", False))
        )
        if conversation is not None:
            if collection_already_active(conversation, lead_draft):
                result = self.lead_capture_service.capture_from_message(
                    conversation=conversation,
                    message=current_message,
                    history=history,
                )
                lead_draft = result.lead_draft
            elif not pure_consultative and not knowledge_during_collection:
                result = self.lead_capture_service.capture_from_message(
                    conversation=conversation,
                    message=current_message,
                    history=history,
                )
                lead_draft = result.lead_draft
                set_state(conversation, LeadState.DISCOVERY)
            elif lead_draft is None and not knowledge_during_collection:
                set_state(conversation, LeadState.DISCOVERY)
        if is_conceptual_price_question(current_message):
            reply = build_conceptual_price_reply(lead_draft, current_message=current_message)
            reply = self._with_knowledge(
                reply,
                knowledge_context,
                conversation=conversation,
                lead_draft=lead_draft,
                current_message=current_message,
                dialogue_memory=dialogue_memory,
            )
        elif has_semantic_knowledge_block(knowledge_context):
            resume_collection = collection_already_active(conversation, lead_draft)
            reply = self._build_grounded_consultative_reply(
                lead_draft=lead_draft,
                conversation=conversation,
                current_message=current_message,
                history=history,
                knowledge_context=knowledge_context,
                append_followup=not resume_collection and not knowledge_during_collection,
                dialogue_memory=dialogue_memory,
            )
            if resume_collection and lead_draft is not None:
                from leads.services.commercial import QualificationService

                pending = QualificationService().promptable_fields(
                    lead_draft,
                    history=history,
                    message=current_message,
                )
                if pending and pending[0] != "need_summary":
                    resume = self.lead_capture_service.build_next_prompt(
                        lead_draft,
                        pending,
                        invalid_fields=[],
                    )
                    if resume and resume.lower() not in reply.lower():
                        reply = f"{reply.rstrip()} {resume}".strip()
        elif self._should_answer_informatively_from_knowledge(discovery, knowledge_context):
            reply = self._grounded_informational_followup(
                assistant_profile=assistant_profile,
                knowledge_context=knowledge_context,
                lead_draft=lead_draft,
                current_message=current_message,
                history=history,
                dialogue_memory=dialogue_memory,
            )
        else:
            reply = build_consultative_commercial_reply(
                lead_draft=lead_draft,
                current_message=current_message,
                history=history,
            )
            reply = self._with_knowledge(
                reply,
                knowledge_context,
                conversation=conversation,
                lead_draft=lead_draft,
                current_message=current_message,
                dialogue_memory=dialogue_memory,
            )
        decision = LiviaReply(intent=intent, reply=reply)
        if not pure_consultative and not knowledge_during_collection:
            decision = self._finalize_handoff(decision, conversation, lead_draft, discovery, current_message)
        return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)

    def _grounded_informational_followup(
        self,
        *,
        assistant_profile,
        knowledge_context: str,
        lead_draft,
        current_message: str,
        history=None,
        dialogue_memory=None,
    ) -> str:
        """Grounding RAG + follow-up curto por domínio/aplicação (sem misturar verticais)."""
        conversation = getattr(lead_draft, "conversation", None) if lead_draft is not None else None
        return self._build_grounded_consultative_reply(
            lead_draft=lead_draft,
            conversation=conversation,
            current_message=current_message,
            history=history,
            knowledge_context=knowledge_context,
            append_followup=True,
            dialogue_memory=dialogue_memory,
        )

    def _handle_qualification(
        self,
        *,
        intent: str,
        history: Iterable[dict[str, str]],
        current_message: str,
        conversation,
        discovery=None,
        assistant_profile=None,
        knowledge_context: str = "",
        activate_collection: bool = False,
        collection_reason: str = "",
    ) -> LiviaReply:
        if conversation is None:
            decision = LiviaReply(intent=intent, reply=build_contextual_reply(intent=intent))
            return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)
        if should_block_dialogue_for_locked_lead(conversation, current_message, discovery):
            decision = LiviaReply(
                intent=intent,
                reply="Perfeito, já encaminhei seus dados para sequência do atendimento. Se for uma nova demanda, me diga que é um novo pedido.",
            )
            return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)

        if activate_collection:
            lead_seed = self.lead_capture_service.get_or_create_lead_draft(conversation)
            mark_collection_active(lead_seed, reason=collection_reason)

        result = self.lead_capture_service.capture_from_message(
            conversation=conversation,
            message=current_message,
            history=history,
        )
        if activate_collection:
            mark_collection_active(result.lead_draft, reason=collection_reason)
        from assistant_core.consultative_policy import is_explicit_human_handoff

        if is_explicit_human_handoff(current_message):
            reply = self._human_handoff_collection_reply(result.lead_draft, result.missing_fields)
        else:
            reply = self.lead_capture_service.build_next_prompt(
                result.lead_draft,
                result.missing_fields,
                intent=intent,
                invalid_fields=result.invalid_fields,
            )
        from leads.services.commercial import is_ready_for_commercial_notification

        if is_ready_for_commercial_notification(result.lead_draft) or (
            result.is_qualified and not result.missing_fields
        ):
            reply = build_contextual_reply(intent=intent, missing_fields=[])
        decision = LiviaReply(intent=intent, reply=reply, collection_prompt=True)
        decision = self._finalize_handoff(decision, conversation, result.lead_draft, discovery, current_message)
        return self._finalize_ai_response(decision, conversation, assistant_profile, discovery, current_message, history, knowledge_context)

    def _human_handoff_collection_reply(self, lead_draft, missing_fields: list[str]) -> str:
        missing_fields = list(missing_fields or [])
        has_contact = self.lead_capture_service._has_phone_or_email(lead_draft)
        if not has_contact:
            return ""
        if "name" in missing_fields:
            return "Ótimo. Para eu dar sequência, qual é o seu nome ou o nome da empresa?"
        return ""

    def _should_answer_informatively_from_knowledge(self, discovery, knowledge_context: str) -> bool:
        return has_semantic_knowledge_block(knowledge_context) and is_informational_knowledge_query(discovery)

    def _should_start_lead_from_contact(
        self,
        current_message: str,
        has_commercial_interest: bool,
        has_quote_request: bool,
    ) -> bool:
        if has_commercial_interest or has_quote_request:
            return True
        text = str(current_message or "").strip().lower()
        if not text or not has_basic_contact(current_message):
            return False
        return True
