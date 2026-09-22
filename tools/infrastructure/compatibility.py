from __future__ import annotations

TOOL_AGENT_COMPATIBILITY = {
    "prospecting.build_search_plan": frozenset({"prospecting"}),
    "prospecting.external_search_probe": frozenset({"prospecting"}),
    "prospecting.search_google_maps": frozenset({"prospecting"}),
}


def is_tool_compatible_with_agent(*, tool_slug: str, agent_slug: str) -> bool:
    allowed_agents = TOOL_AGENT_COMPATIBILITY.get(str(tool_slug or ""))
    if allowed_agents is None:
        return False
    return str(agent_slug or "") in allowed_agents
