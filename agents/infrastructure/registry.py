from agents.application.registry import AgentRuntimeRegistry
from agents.infrastructure.livia_adapter import LiviaAgentAdapter
from agents.infrastructure.prospecting_adapter import ProspectingAgentAdapter


def build_default_registry() -> AgentRuntimeRegistry:
    registry = AgentRuntimeRegistry()
    registry.register("livia", LiviaAgentAdapter)
    registry.register("prospecting", ProspectingAgentAdapter)
    return registry
