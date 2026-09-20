from tools.application.registry import ToolRuntimeRegistry
from tools.infrastructure.prospecting_build_search_plan import ProspectingBuildSearchPlanTool


def build_default_tool_registry() -> ToolRuntimeRegistry:
    registry = ToolRuntimeRegistry()
    registry.register("prospecting_build_search_plan", ProspectingBuildSearchPlanTool)
    return registry
