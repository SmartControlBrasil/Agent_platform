from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from operations_portal.access import portal_template_context, require_portal_capability, resolve_portal_access
from operations_portal.forms import ExecutorPairingClaimForm
from tenants.access import CAPABILITY_TENANT_MANAGE, CAPABILITY_TENANT_VIEW
from tools.application.executor_pairing_claims import TenantPairingClaimError, claim_pairing_for_tenant
from tools.application.executor_presence import executor_presence
from tools.models import ToolExecution, ToolExecutor


@login_required
def executor_management(request):
    access = resolve_portal_access(request, capability=CAPABILITY_TENANT_VIEW, require_tenant=True)
    tenant = access.tenant
    executors = (
        ToolExecutor.objects.filter(tenant=tenant)
        .prefetch_related("capabilities__tool_definition")
        .order_by("name", "created_at")
    )
    rows = [_executor_row(executor) for executor in executors]
    context = {
        **portal_template_context(access),
        "active_section": "executores",
        "title": "Executores",
        "executors": rows,
        "pairing_form": ExecutorPairingClaimForm(),
        "can_manage_executors": CAPABILITY_TENANT_MANAGE in access.capabilities,
    }
    return render(request, "operations_portal/executors/index.html", context)


@login_required
@require_POST
def executor_pairing_claim(request):
    if "tenant" in request.POST:
        raise PermissionDenied("Troca de tenant não é permitida nesta ação.")
    access = resolve_portal_access(request, capability=CAPABILITY_TENANT_VIEW, require_tenant=True)
    require_portal_capability(access, CAPABILITY_TENANT_MANAGE)
    form = ExecutorPairingClaimForm(request.POST)
    if form.is_valid():
        try:
            result = claim_pairing_for_tenant(
                pairing_code=form.cleaned_data["pairing_code"],
                tenant=access.tenant,
                actor=request.user,
                request=request,
            )
        except (TenantPairingClaimError, ValidationError) as exc:
            messages.error(request, _validation_message(exc))
        else:
            executor = result.pairing.executor
            messages.success(request, f"Executor {executor.name} vinculado ao tenant ativo.")
    else:
        messages.error(request, "Informe um código de pareamento válido.")
    return redirect("operations_portal:executor_management")


def _executor_row(executor: ToolExecutor) -> dict:
    presence = executor_presence(executor)
    capabilities = [
        capability.tool_definition
        for capability in executor.capabilities.all()
        if capability.is_enabled and capability.tool_definition.is_active
    ]
    last_execution = (
        ToolExecution.objects.filter(executor=executor)
        .select_related("tool_definition")
        .order_by("-created_at")
        .first()
    )
    return {
        "executor": executor,
        "presence": presence,
        "capabilities": capabilities,
        "last_execution": last_execution,
    }


def _validation_message(exc: ValidationError) -> str:
    messages_list = getattr(exc, "messages", None)
    if messages_list:
        return " ".join(str(message) for message in messages_list)
    return str(exc)
