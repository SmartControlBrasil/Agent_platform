from __future__ import annotations

from typing import Any, Callable

from django.core.exceptions import ValidationError

from tools.infrastructure.google_maps_contract import (
    summarize_google_maps_execution,
    validate_google_maps_input,
    validate_google_maps_result,
)
from tools.models import ToolExecution

InputValidator = Callable[[dict[str, Any]], dict[str, Any]]
ResultValidator = Callable[[dict[str, Any], ToolExecution], dict[str, Any]]
SummaryBuilder = Callable[[ToolExecution], dict[str, Any]]


class ToolInputValidatorRegistry:
    def __init__(self):
        self._validators: dict[str, InputValidator] = {}

    def register(self, tool_slug: str, validator: InputValidator) -> None:
        self._validators[tool_slug] = validator

    def validate(self, *, tool_slug: str, payload: dict[str, Any]) -> dict[str, Any]:
        validator = self._validators.get(tool_slug)
        if validator is None:
            return dict(payload or {})
        return validator(payload)


class ToolResultValidatorRegistry:
    def __init__(self):
        self._validators: dict[str, ResultValidator] = {}
        self._summaries: dict[str, SummaryBuilder] = {}

    def register(
        self,
        tool_slug: str,
        validator: ResultValidator,
        *,
        summary_builder: SummaryBuilder | None = None,
    ) -> None:
        self._validators[tool_slug] = validator
        if summary_builder is not None:
            self._summaries[tool_slug] = summary_builder

    def validate(self, *, tool_slug: str, result: dict[str, Any], execution: ToolExecution) -> dict[str, Any]:
        validator = self._validators.get(tool_slug)
        if validator is None:
            return dict(result or {})
        return validator(result, execution)

    def summarize(self, execution: ToolExecution) -> dict[str, Any] | None:
        builder = self._summaries.get(execution.tool_definition.slug)
        if builder is None:
            return None
        return builder(execution)


def build_default_input_validator_registry() -> ToolInputValidatorRegistry:
    registry = ToolInputValidatorRegistry()
    registry.register("prospecting.search_google_maps", validate_google_maps_input)
    return registry


def build_default_result_validator_registry() -> ToolResultValidatorRegistry:
    registry = ToolResultValidatorRegistry()
    registry.register(
        "prospecting.search_google_maps",
        validate_google_maps_result,
        summary_builder=summarize_google_maps_execution,
    )
    return registry


def summarize_tool_execution(execution: ToolExecution) -> dict[str, Any] | None:
    return build_default_result_validator_registry().summarize(execution)


def validation_error_message(exc: ValidationError) -> str:
    if hasattr(exc, "messages"):
        return " ".join(str(message) for message in exc.messages)
    return str(exc)
