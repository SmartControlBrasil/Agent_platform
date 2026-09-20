from __future__ import annotations

from collections.abc import Callable

from agents.domain.runtime import AgentRuntimePort


class UnknownAgentRuntime(RuntimeError):
    pass


class AgentRuntimeRegistry:
    def __init__(self):
        self._factories: dict[str, Callable[[], AgentRuntimePort]] = {}

    def register(self, handler: str, factory: Callable[[], AgentRuntimePort]) -> None:
        normalized = str(handler or "").strip()
        if not normalized:
            raise ValueError("runtime handler is required")
        self._factories[normalized] = factory

    def resolve(self, handler: str) -> AgentRuntimePort:
        normalized = str(handler or "").strip()
        try:
            factory = self._factories[normalized]
        except KeyError as exc:
            raise UnknownAgentRuntime(f"Unknown agent runtime handler: {normalized}") from exc
        return factory()

    def registered_handlers(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))
