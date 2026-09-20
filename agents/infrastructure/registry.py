from agents.application.registry import AgentRuntimeRegistry
from agents.infrastructure.livia_adapter import LiviaAgentAdapter


def build_default_registry() -> AgentRuntimeRegistry:
    registry = AgentRuntimeRegistry()
    registry.register("livia", LiviaAgentAdapter)
    return registry
