from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolExecutionContext:
    tenant_id: str
    project_id: str
    installation_id: str
    tool_binding_id: str
    user_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolRequest:
    input: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolRuntimePort(Protocol):
    def execute(self, context: ToolExecutionContext, request: ToolRequest) -> ToolResult:
        ...
