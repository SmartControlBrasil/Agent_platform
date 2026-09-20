from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class AgentExecutionContext:
    tenant_id: str
    project_id: str
    installation_id: str
    user_id: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRequest:
    input: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResponse:
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"


class AgentRuntimePort(Protocol):
    def execute(self, context: AgentExecutionContext, request: AgentRequest) -> AgentResponse:
        ...
