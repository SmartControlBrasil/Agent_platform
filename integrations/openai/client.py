from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenAIChatResult:
    text: str
    success: bool
    dry_run: bool
    error_type: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OpenAIChatClient:
    endpoint = "https://api.openai.com/v1/chat/completions"

    def create_chat_completion(self, *, messages: list[dict[str, str]]) -> OpenAIChatResult:
        enabled = bool(getattr(settings, "LIVIA_AI_ENABLED", False))
        dry_run = bool(getattr(settings, "LIVIA_AI_DRY_RUN", True))
        model = str(getattr(settings, "LIVIA_OPENAI_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini")
        api_key = str(getattr(settings, "LIVIA_OPENAI_API_KEY", "") or "").strip()
        timeout = int(getattr(settings, "LIVIA_OPENAI_TIMEOUT_SECONDS", 8) or 8)
        max_tokens = int(getattr(settings, "LIVIA_OPENAI_MAX_OUTPUT_TOKENS", 350) or 350)
        temperature = float(getattr(settings, "LIVIA_OPENAI_TEMPERATURE", 0.3) or 0.3)

        if not enabled or dry_run or not api_key:
            logger.info(
                "livia_ai_skip enabled=%s dry_run=%s model=%s has_api_key=%s",
                enabled,
                dry_run,
                model,
                bool(api_key),
            )
            return OpenAIChatResult(text="", success=False, dry_run=True)

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            text = str(data["choices"][0]["message"]["content"] or "").strip()
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
            if not text:
                logger.info("livia_ai_empty_response model=%s", model)
                return OpenAIChatResult(
                    text="",
                    success=False,
                    dry_run=False,
                    error_type="empty_response",
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )
            logger.info(
                "livia_ai_success model=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s",
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
            )
            return OpenAIChatResult(
                text=text,
                success=True,
                dry_run=False,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except requests.Timeout:
            logger.warning("livia_ai_failure model=%s error_type=Timeout", model)
            return OpenAIChatResult(
                text="",
                success=False,
                dry_run=False,
                error_type="Timeout",
                model=model,
            )
        except Exception as exc:  # pragma: no cover - defensive provider guard
            logger.warning("livia_ai_failure model=%s error_type=%s", model, exc.__class__.__name__)
            return OpenAIChatResult(
                text="",
                success=False,
                dry_run=False,
                error_type=exc.__class__.__name__,
                model=model,
            )

    def create_json_chat_completion(self, *, messages: list[dict[str, str]]) -> OpenAIChatResult:
        """Completion curta em JSON — reutiliza credenciais/timeout do cliente principal."""
        enabled = bool(getattr(settings, "LIVIA_AI_ENABLED", False))
        dry_run = bool(getattr(settings, "LIVIA_AI_DRY_RUN", True))
        model = str(getattr(settings, "LIVIA_OPENAI_MODEL", "gpt-4.1-mini") or "gpt-4.1-mini")
        api_key = str(getattr(settings, "LIVIA_OPENAI_API_KEY", "") or "").strip()
        timeout = int(getattr(settings, "LIVIA_SEMANTIC_INTENT_TIMEOUT_SECONDS", 4) or 4)
        max_tokens = int(getattr(settings, "LIVIA_SEMANTIC_INTENT_MAX_OUTPUT_TOKENS", 120) or 120)
        temperature = float(getattr(settings, "LIVIA_SEMANTIC_INTENT_TEMPERATURE", 0.0) or 0.0)

        if not enabled or dry_run or not api_key:
            return OpenAIChatResult(text="", success=False, dry_run=True, error_type="disabled", model=model)

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            text = str(data["choices"][0]["message"]["content"] or "").strip()
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
            if not text:
                return OpenAIChatResult(
                    text="",
                    success=False,
                    dry_run=False,
                    error_type="empty_response",
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )
            return OpenAIChatResult(
                text=text,
                success=True,
                dry_run=False,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except requests.Timeout:
            return OpenAIChatResult(text="", success=False, dry_run=False, error_type="Timeout", model=model)
        except Exception as exc:  # pragma: no cover
            return OpenAIChatResult(
                text="",
                success=False,
                dry_run=False,
                error_type=exc.__class__.__name__,
                model=model,
            )
