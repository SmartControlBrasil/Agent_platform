from __future__ import annotations

from collections.abc import Callable

from tools.domain.runtime import ToolRuntimePort


class UnknownToolRuntime(RuntimeError):
    pass


class ToolRuntimeRegistry:
    def __init__(self):
        self._factories: dict[str, Callable[[], ToolRuntimePort]] = {}

    def register(self, handler: str, factory: Callable[[], ToolRuntimePort]) -> None:
        normalized = str(handler or "").strip()
        if not normalized:
            raise ValueError("tool runtime handler is required")
        self._factories[normalized] = factory

    def resolve(self, handler: str) -> ToolRuntimePort:
        normalized = str(handler or "").strip()
        try:
            factory = self._factories[normalized]
        except KeyError as exc:
            raise UnknownToolRuntime(f"Unknown tool runtime handler: {normalized}") from exc
        return factory()

    def registered_handlers(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))
