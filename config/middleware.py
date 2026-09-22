from django.http import HttpResponse

from tenants.origins import (
    log_origin_block,
    resolve_public_tenant,
    patch_public_cors_headers,
    validate_tenant_origin,
)


class LiviaWidgetCorsMiddleware:
    CORS_PATHS = {"/api/chat/", "/api/widget/config/"}
    EXECUTOR_API_PREFIX = "/api/v1/executors/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(self.EXECUTOR_API_PREFIX) and request.method == "OPTIONS":
            response = self._handle_executor_preflight(request)
        elif request.path in self.CORS_PATHS and request.method == "OPTIONS":
            response = self._handle_preflight(request)
        else:
            response = self.get_response(request)

        if request.path.startswith(self.EXECUTOR_API_PREFIX):
            self._patch_executor_cors_headers(request, response)
        cors_origin = getattr(request, "livia_validated_origin", "")
        if request.path in self.CORS_PATHS and cors_origin:
            patch_public_cors_headers(response, cors_origin)
        return response

    def _handle_preflight(self, request):
        resolution = resolve_public_tenant(request)
        if resolution.error == "tenant_mismatch":
            return HttpResponse(status=400)
        if resolution.error:
            return HttpResponse(status=403)

        result = validate_tenant_origin(request, resolution.tenant)
        if not result.allowed:
            log_origin_block(resolution.tenant, result)
            return HttpResponse(status=403)
        request.livia_validated_origin = result.origin
        response = HttpResponse(status=204)
        patch_public_cors_headers(response, result.origin)
        return response

    def _handle_executor_preflight(self, request):
        response = HttpResponse(status=204 if self._executor_origin(request) else 403)
        self._patch_executor_cors_headers(request, response)
        return response

    def _patch_executor_cors_headers(self, request, response):
        origin = self._executor_origin(request)
        if not origin:
            return
        response["Access-Control-Allow-Origin"] = origin
        response["Vary"] = "Origin"
        response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
        response["Access-Control-Max-Age"] = "600"

    def _executor_origin(self, request):
        origin = request.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            return origin
        return ""
