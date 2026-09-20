from __future__ import annotations

from django.core.exceptions import ValidationError

from tools.domain.runtime import ToolExecutionContext, ToolRequest, ToolResult

DEFAULT_MAX_QUERIES = 8
MIN_MAX_QUERIES = 1
MAX_MAX_QUERIES = 20


def normalize_build_search_plan_configuration(configuration: dict) -> dict[str, int]:
    if not isinstance(configuration, dict):
        raise ValidationError("Build search plan configuration must be an object.")
    raw = configuration.get("max_queries", DEFAULT_MAX_QUERIES)
    try:
        max_queries = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"max_queries": "max_queries must be an integer."}) from exc
    if not MIN_MAX_QUERIES <= max_queries <= MAX_MAX_QUERIES:
        raise ValidationError({"max_queries": "max_queries must be between 1 and 20."})
    return {"max_queries": max_queries}


class ProspectingBuildSearchPlanTool:
    def execute(self, context: ToolExecutionContext, request: ToolRequest) -> ToolResult:
        config = normalize_build_search_plan_configuration(request.metadata.get("binding_configuration") or {})
        payload = dict(request.input or {})
        target_market = _clean(payload.get("target_market"))
        target_region = _clean(payload.get("target_region"))
        target_profile = _clean(payload.get("target_profile"))
        objective = _clean(payload.get("objective"))
        base_market = target_profile or target_market or "prospects"
        region = target_region or "target region"
        query_candidates = [
            f"{base_market} {region}",
            f"{target_market or base_market} {region}",
            f"{base_market} empresas {region}",
            f"{base_market} decisores {region}",
            f"{base_market} fornecedores {region}",
            f"{base_market} diretoria {region}",
            f"{base_market} comercial {region}",
            f"{base_market} oportunidades {region}",
            f"{target_market or base_market} privado {region}",
            f"{target_market or base_market} lista {region}",
            f"{base_market} mercado {region}",
            f"{base_market} expansão {region}",
            f"{base_market} B2B {region}",
            f"{base_market} compradores {region}",
            f"{base_market} associações {region}",
            f"{base_market} ranking {region}",
            f"{base_market} eventos {region}",
            f"{base_market} oportunidades comerciais {region}",
            f"{base_market} contatos {region}",
            f"{base_market} unidades {region}",
        ]
        queries = []
        for query in query_candidates:
            normalized = " ".join(query.split())
            if normalized not in queries:
                queries.append(normalized)
            if len(queries) >= config["max_queries"]:
                break
        return ToolResult(
            status="planned",
            output={
                "status": "planned",
                "queries": queries,
                "target_market": target_market,
                "target_region": target_region,
                "target_profile": target_profile,
                "objective": objective,
            },
            metadata={
                "tool": "prospecting.build_search_plan",
                "max_queries": config["max_queries"],
                "tenant_id": context.tenant_id,
                "project_id": context.project_id,
                "installation_id": context.installation_id,
                "tool_binding_id": context.tool_binding_id,
            },
        )


def _clean(value) -> str:
    return " ".join(str(value or "").strip().split())
