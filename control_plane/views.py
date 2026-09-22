from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from agents.application.installations import (
    disable_agent_installation,
    enable_agent_installation,
    install_agent as install_agent_service,
    update_installation_configuration,
)
from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from tools.forms import AgentToolBindingForm
from tools.infrastructure.validation import summarize_tool_execution
from tools.application.identity import approve_pairing, reject_pairing, revoke_credential, rotate_credential
from tools.models import (
    AgentToolBinding,
    ToolDefinition,
    ToolExecution,
    ToolExecutor,
    ToolExecutorCapability,
    ToolExecutorCredential,
    ToolExecutorPairingRequest,
)
from audit.services import audit_model_snapshot, record_audit_event
from projects.models import Project
from tenants.access import (
    CAPABILITY_TENANT_MANAGE,
    CAPABILITY_TENANT_VIEW,
    get_accessible_tenants,
    user_has_tenant_capability,
)
from tenants.models import Tenant

from .forms import InstallationConfigurationForm, InstallAgentForm, ProjectForm

ACTION_PROJECT_CREATED = "project.created"
ACTION_AGENT_INSTALLED = "agent.installed"
ACTION_AGENT_ENABLED = "agent.enabled"
ACTION_AGENT_DISABLED = "agent.disabled"
ACTION_AGENT_CONFIGURATION_UPDATED = "agent.configuration.updated"
ACTION_TOOL_BOUND = "tool.bound"
ACTION_TOOL_ENABLED = "tool.enabled"
ACTION_TOOL_DISABLED = "tool.disabled"
ACTION_TOOL_CONFIGURATION_UPDATED = "tool.configuration.updated"
ACTION_EXECUTOR_CAPABILITY_ADDED = "tool_executor.capability.added"
ACTION_EXECUTOR_CAPABILITY_ENABLED = "tool_executor.capability.enabled"
ACTION_EXECUTOR_CAPABILITY_DISABLED = "tool_executor.capability.disabled"


def _accessible_tenants(user):
    return get_accessible_tenants(user)


def _manageable_tenants(user):
    tenants = _accessible_tenants(user)
    if user.is_superuser:
        return tenants
    return [tenant for tenant in tenants if user_has_tenant_capability(user, tenant, CAPABILITY_TENANT_MANAGE)]


def _tenant_ids(user):
    return list(_accessible_tenants(user).values_list("id", flat=True))


def _can_manage(user, tenant):
    return user_has_tenant_capability(user, tenant, CAPABILITY_TENANT_MANAGE)


def _require_manage(user, tenant):
    if not _can_manage(user, tenant):
        raise PermissionDenied


def _project_queryset(user):
    return Project.objects.filter(tenant_id__in=_tenant_ids(user)).select_related("tenant")


def _installation_queryset(user):
    return AgentInstallation.objects.filter(tenant_id__in=_tenant_ids(user)).select_related(
        "tenant", "project", "agent_definition", "agent_version"
    )


def _tool_binding_queryset(user):
    return AgentToolBinding.objects.filter(tenant_id__in=_tenant_ids(user)).select_related(
        "tenant", "project", "agent_installation", "tool_definition", "agent_installation__agent_definition"
    )


def _tool_execution_queryset(user):
    return ToolExecution.objects.filter(tenant_id__in=_tenant_ids(user)).select_related(
        "tenant", "project", "agent_installation", "tool_definition", "executor"
    )


def _executor_queryset(user):
    return ToolExecutor.objects.filter(tenant_id__in=_tenant_ids(user)).select_related("tenant").prefetch_related(
        "capabilities__tool_definition", "credentials"
    )


def _pairing_queryset(user):
    qs = ToolExecutorPairingRequest.objects.select_related("tenant", "executor", "approved_by")
    if user.is_superuser:
        return qs
    return qs.filter(tenant_id__in=_tenant_ids(user)) | qs.filter(tenant__isnull=True)


def _credential_queryset(user):
    return ToolExecutorCredential.objects.select_related("executor", "executor__tenant").filter(
        executor__tenant_id__in=_tenant_ids(user)
    )


def _executor_status(executor):
    if executor.last_seen_at is None:
        return "offline"
    age = timezone.now() - executor.last_seen_at
    if age <= timezone.timedelta(minutes=5):
        return "online"
    if age <= timezone.timedelta(minutes=30):
        return "recent"
    return "offline"


def _base_context(active_section):
    return {"active_section": active_section, "brand_name": "Agent Platform"}


@login_required(login_url="/admin/login/")
def dashboard(request):
    tenants = _accessible_tenants(request.user)
    tenant_ids = list(tenants.values_list("id", flat=True))
    projects = Project.objects.filter(tenant_id__in=tenant_ids)
    installations = AgentInstallation.objects.filter(tenant_id__in=tenant_ids)
    context = _base_context("dashboard")
    context.update(
        {
            "summary": {
                "tenants": tenants.count(),
                "projects": projects.filter(is_active=True).count(),
                "agents": AgentDefinition.objects.filter(is_active=True).count(),
                "installations": installations.count(),
                "enabled_installations": installations.filter(is_enabled=True).count(),
                "disabled_installations": installations.filter(is_enabled=False).count(),
            },
            "has_data": tenants.exists() or AgentDefinition.objects.exists(),
        }
    )
    return render(request, "control_plane/dashboard.html", context)


@login_required(login_url="/admin/login/")
def tenant_list(request):
    tenants = _accessible_tenants(request.user).annotate(
        project_count=Count("projects", distinct=True),
        installation_count=Count("agent_installations", distinct=True),
    )
    context = _base_context("tenants")
    context.update({"tenants": tenants})
    return render(request, "control_plane/tenant_list.html", context)


@login_required(login_url="/admin/login/")
def tenant_detail(request, pk):
    tenant = get_object_or_404(
        _accessible_tenants(request.user).annotate(
            project_count=Count("projects", distinct=True),
            installation_count=Count("agent_installations", distinct=True),
        ),
        pk=pk,
    )
    context = _base_context("tenants")
    context.update(
        {
            "tenant": tenant,
            "projects": Project.objects.filter(tenant=tenant).order_by("name"),
            "installations": AgentInstallation.objects.filter(tenant=tenant).select_related(
                "project", "agent_definition", "agent_version"
            ),
            "can_manage": _can_manage(request.user, tenant),
        }
    )
    return render(request, "control_plane/tenant_detail.html", context)


@login_required(login_url="/admin/login/")
def project_list(request):
    projects = _project_queryset(request.user).annotate(installation_count=Count("agent_installations"))
    context = _base_context("projects")
    context.update({"projects": projects, "can_create": bool(_manageable_tenants(request.user))})
    return render(request, "control_plane/project_list.html", context)


@login_required(login_url="/admin/login/")
def project_create(request):
    tenants = _manageable_tenants(request.user)
    if not tenants:
        raise PermissionDenied
    if request.method == "POST":
        form = ProjectForm(request.POST, tenants=Tenant.objects.filter(pk__in=[tenant.pk for tenant in tenants]))
        if form.is_valid():
            tenant = form.cleaned_data["tenant"]
            _require_manage(request.user, tenant)
            project = form.save()
            record_audit_event(
                action=ACTION_PROJECT_CREATED,
                actor=request.user,
                tenant=tenant,
                obj=project,
                after_data=audit_model_snapshot(project, fields=["tenant", "name", "slug", "description", "is_active"]),
                request=request,
            )
            messages.success(request, "Project criado com sucesso.")
            return redirect("control_plane:project_detail", pk=project.pk)
    else:
        form = ProjectForm(tenants=Tenant.objects.filter(pk__in=[tenant.pk for tenant in tenants]), initial={"is_active": True})
    context = _base_context("projects")
    context.update({"form": form})
    return render(request, "control_plane/project_form.html", context)


@login_required(login_url="/admin/login/")
def project_detail(request, pk):
    project = get_object_or_404(_project_queryset(request.user), pk=pk)
    context = _base_context("projects")
    context.update(
        {
            "project": project,
            "installations": AgentInstallation.objects.filter(project=project).select_related(
                "agent_definition", "agent_version"
            ),
            "can_manage": _can_manage(request.user, project.tenant),
        }
    )
    return render(request, "control_plane/project_detail.html", context)


@login_required(login_url="/admin/login/")
def agent_list(request):
    agents = AgentDefinition.objects.prefetch_related("versions").order_by("name")
    context = _base_context("agents")
    context.update({"agents": agents})
    return render(request, "control_plane/agent_list.html", context)


@login_required(login_url="/admin/login/")
def agent_detail(request, pk):
    agent = get_object_or_404(AgentDefinition.objects.prefetch_related("versions"), pk=pk)
    context = _base_context("agents")
    context.update({"agent": agent})
    return render(request, "control_plane/agent_detail.html", context)


@login_required(login_url="/admin/login/")
def install_agent(request, project_id):
    project = get_object_or_404(_project_queryset(request.user), pk=project_id)
    _require_manage(request.user, project.tenant)
    if request.method == "POST":
        form = InstallAgentForm(request.POST)
        if form.is_valid():
            try:
                installation = install_agent_service(
                    tenant=project.tenant,
                    project=project,
                    agent_definition=form.cleaned_data["agent_definition"],
                    agent_version=form.cleaned_data["agent_version"],
                    name=form.cleaned_data.get("name") or form.cleaned_data["agent_definition"].name,
                    configuration=form.configuration(),
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                record_audit_event(
                    action=ACTION_AGENT_INSTALLED,
                    actor=request.user,
                    tenant=project.tenant,
                    obj=installation,
                    after_data=audit_model_snapshot(
                        installation,
                        fields=["tenant", "project", "agent_definition", "agent_version", "name", "is_enabled", "configuration"],
                    ),
                    request=request,
                )
                messages.success(request, "Agente instalado com sucesso.")
                return redirect("control_plane:installation_detail", pk=installation.pk)
    else:
        form = InstallAgentForm(initial={"name": "", "agent_definition": request.GET.get("agent_definition", "")})
    context = _base_context("projects")
    context.update({"project": project, "form": form})
    return render(request, "control_plane/install_agent.html", context)


@login_required(login_url="/admin/login/")
def installation_list(request):
    installations = _installation_queryset(request.user).order_by("tenant__name", "project__name", "name")
    context = _base_context("installations")
    context.update({"installations": installations})
    return render(request, "control_plane/installation_list.html", context)


@login_required(login_url="/admin/login/")
def installation_detail(request, pk):
    installation = get_object_or_404(_installation_queryset(request.user), pk=pk)
    can_manage = _can_manage(request.user, installation.tenant)
    if request.method == "POST":
        _require_manage(request.user, installation.tenant)
        form = InstallationConfigurationForm(request.POST, instance=installation)
        if form.is_valid():
            before_config = dict(installation.configuration or {})
            installation = update_installation_configuration(installation=installation, configuration=form.configuration())
            after_config = dict(installation.configuration or {})
            changed_config_fields = sorted(
                key for key in set(before_config) | set(after_config) if before_config.get(key) != after_config.get(key)
            )
            record_audit_event(
                action=ACTION_AGENT_CONFIGURATION_UPDATED,
                actor=request.user,
                tenant=installation.tenant,
                obj=installation,
                metadata={
                    "installation_id": str(installation.pk),
                    "changed_fields": changed_config_fields,
                },
                request=request,
            )
            messages.success(request, "Configuração atualizada.")
            return redirect("control_plane:installation_detail", pk=installation.pk)
    else:
        form = InstallationConfigurationForm(instance=installation)
    tool_bindings = installation.tool_bindings.select_related("tool_definition").order_by("tool_definition__category", "tool_definition__name")
    context = _base_context("installations")
    context.update(
        {
            "installation": installation,
            "form": form,
            "can_manage": can_manage,
            "tool_bindings": tool_bindings,
        }
    )
    return render(request, "control_plane/installation_detail.html", context)


@require_POST
@login_required(login_url="/admin/login/")
def installation_enable(request, pk):
    installation = get_object_or_404(_installation_queryset(request.user), pk=pk)
    _require_manage(request.user, installation.tenant)
    installation = enable_agent_installation(installation=installation)
    record_audit_event(action=ACTION_AGENT_ENABLED, actor=request.user, tenant=installation.tenant, obj=installation, request=request)
    messages.success(request, "Instalação habilitada.")
    return redirect("control_plane:installation_detail", pk=installation.pk)


@require_POST
@login_required(login_url="/admin/login/")
def installation_disable(request, pk):
    installation = get_object_or_404(_installation_queryset(request.user), pk=pk)
    _require_manage(request.user, installation.tenant)
    installation = disable_agent_installation(installation=installation)
    record_audit_event(action=ACTION_AGENT_DISABLED, actor=request.user, tenant=installation.tenant, obj=installation, request=request)
    messages.success(request, "Instalação desabilitada.")
    return redirect("control_plane:installation_detail", pk=installation.pk)


@login_required(login_url="/admin/login/")
def tool_list(request):
    tools = ToolDefinition.objects.order_by("category", "name")
    context = _base_context("tools")
    context.update({"tools": tools})
    return render(request, "control_plane/tool_list.html", context)


@login_required(login_url="/admin/login/")
def tool_detail(request, pk):
    tool = get_object_or_404(ToolDefinition.objects.all(), pk=pk)
    bindings = _tool_binding_queryset(request.user).filter(tool_definition=tool).order_by(
        "tenant__name", "project__name", "agent_installation__name"
    )
    context = _base_context("tools")
    context.update({"tool": tool, "bindings": bindings, "executor_requirement": "Browser executor" if tool.execution_mode == ToolDefinition.ExecutionMode.DELEGATED else ""})
    return render(request, "control_plane/tool_detail.html", context)


@login_required(login_url="/admin/login/")
def installation_add_tool(request, installation_id):
    installation = get_object_or_404(_installation_queryset(request.user), pk=installation_id)
    _require_manage(request.user, installation.tenant)
    if request.method == "POST":
        form = AgentToolBindingForm(request.POST, installation=installation)
        if form.is_valid():
            binding = AgentToolBinding(
                tenant=installation.tenant,
                project=installation.project,
                agent_installation=installation,
                tool_definition=form.cleaned_data["tool_definition"],
                is_enabled=form.cleaned_data["is_enabled"],
                configuration=form.configuration(),
            )
            try:
                binding.save()
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                record_audit_event(
                    action=ACTION_TOOL_BOUND,
                    actor=request.user,
                    tenant=installation.tenant,
                    obj=binding,
                    after_data=audit_model_snapshot(
                        binding,
                        fields=["tenant", "project", "agent_installation", "tool_definition", "is_enabled", "configuration"],
                    ),
                    request=request,
                )
                messages.success(request, "Tool adicionada à instalação.")
                return redirect("control_plane:installation_detail", pk=installation.pk)
    else:
        form = AgentToolBindingForm(installation=installation)
    context = _base_context("installations")
    context.update({"installation": installation, "form": form, "mode": "add"})
    return render(request, "control_plane/tool_binding_form.html", context)


@login_required(login_url="/admin/login/")
def tool_binding_edit(request, pk):
    binding = get_object_or_404(_tool_binding_queryset(request.user), pk=pk)
    _require_manage(request.user, binding.tenant)
    before_config = dict(binding.configuration or {})
    before_enabled = binding.is_enabled
    if request.method == "POST":
        form = AgentToolBindingForm(request.POST, instance=binding)
        if form.is_valid():
            binding.configuration = form.configuration()
            binding.is_enabled = form.cleaned_data["is_enabled"]
            try:
                binding.save(update_fields=["configuration", "is_enabled", "updated_at"])
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                changed = sorted(
                    key for key in set(before_config) | set(binding.configuration or {})
                    if before_config.get(key) != (binding.configuration or {}).get(key)
                )
                if changed:
                    record_audit_event(
                        action=ACTION_TOOL_CONFIGURATION_UPDATED,
                        actor=request.user,
                        tenant=binding.tenant,
                        obj=binding,
                        metadata={"binding_id": str(binding.pk), "changed_fields": changed},
                        request=request,
                    )
                if before_enabled != binding.is_enabled:
                    record_audit_event(
                        action=ACTION_TOOL_ENABLED if binding.is_enabled else ACTION_TOOL_DISABLED,
                        actor=request.user,
                        tenant=binding.tenant,
                        obj=binding,
                        request=request,
                    )
                messages.success(request, "Tool atualizada.")
                return redirect("control_plane:installation_detail", pk=binding.agent_installation_id)
    else:
        form = AgentToolBindingForm(instance=binding)
    context = _base_context("installations")
    context.update({"installation": binding.agent_installation, "binding": binding, "form": form, "mode": "edit"})
    return render(request, "control_plane/tool_binding_form.html", context)


@require_POST
@login_required(login_url="/admin/login/")
def tool_binding_enable(request, pk):
    binding = get_object_or_404(_tool_binding_queryset(request.user), pk=pk)
    _require_manage(request.user, binding.tenant)
    binding.is_enabled = True
    binding.save(update_fields=["is_enabled", "updated_at"])
    record_audit_event(action=ACTION_TOOL_ENABLED, actor=request.user, tenant=binding.tenant, obj=binding, request=request)
    messages.success(request, "Tool habilitada.")
    return redirect("control_plane:installation_detail", pk=binding.agent_installation_id)


@require_POST
@login_required(login_url="/admin/login/")
def tool_binding_disable(request, pk):
    binding = get_object_or_404(_tool_binding_queryset(request.user), pk=pk)
    _require_manage(request.user, binding.tenant)
    binding.is_enabled = False
    binding.save(update_fields=["is_enabled", "updated_at"])
    record_audit_event(action=ACTION_TOOL_DISABLED, actor=request.user, tenant=binding.tenant, obj=binding, request=request)
    messages.success(request, "Tool desabilitada.")
    return redirect("control_plane:installation_detail", pk=binding.agent_installation_id)


@login_required(login_url="/admin/login/")
def tool_execution_list(request):
    executions = _tool_execution_queryset(request.user).order_by("-created_at")[:200]
    context = _base_context("tool_executions")
    context.update({"executions": executions})
    return render(request, "control_plane/tool_execution_list.html", context)


@login_required(login_url="/admin/login/")
def tool_execution_detail(request, pk):
    execution = get_object_or_404(_tool_execution_queryset(request.user), pk=pk)
    context = _base_context("tool_executions")
    context.update({"execution": execution, "tool_summary": summarize_tool_execution(execution)})
    return render(request, "control_plane/tool_execution_detail.html", context)


@login_required(login_url="/admin/login/")
def executor_list(request):
    executors = _executor_queryset(request.user).order_by("tenant__name", "name")
    context = _base_context("executors")
    context.update({"executors": executors, "executor_status": _executor_status})
    return render(request, "control_plane/executor_list.html", context)


@login_required(login_url="/admin/login/")
def executor_detail(request, pk):
    executor = get_object_or_404(_executor_queryset(request.user), pk=pk)
    can_manage = _can_manage(request.user, executor.tenant)
    assigned_tool_ids = executor.capabilities.values_list("tool_definition_id", flat=True)
    available_capability_tools = ToolDefinition.objects.filter(is_active=True).exclude(pk__in=assigned_tool_ids).order_by("category", "name")
    context = _base_context("executors")
    context.update(
        {
            "executor": executor,
            "status_label": _executor_status(executor),
            "credentials": executor.credentials.order_by("-created_at"),
            "capabilities": executor.capabilities.select_related("tool_definition").order_by("tool_definition__slug"),
            "available_capability_tools": available_capability_tools,
            "can_manage": can_manage,
        }
    )
    return render(request, "control_plane/executor_detail.html", context)


@require_POST
@login_required(login_url="/admin/login/")
def executor_capability_add(request, pk):
    executor = get_object_or_404(_executor_queryset(request.user), pk=pk)
    _require_manage(request.user, executor.tenant)
    tool = get_object_or_404(ToolDefinition.objects.filter(is_active=True), pk=request.POST.get("tool_definition"))
    capability, created = ToolExecutorCapability.objects.get_or_create(
        executor=executor,
        tool_definition=tool,
        defaults={"is_enabled": True},
    )
    if not created and not capability.is_enabled:
        capability.is_enabled = True
        capability.save(update_fields=["is_enabled", "updated_at"])
    record_audit_event(
        action=ACTION_EXECUTOR_CAPABILITY_ADDED if created else ACTION_EXECUTOR_CAPABILITY_ENABLED,
        actor=request.user,
        tenant=executor.tenant,
        obj=capability,
        request=request,
    )
    messages.success(request, "Capability adicionada ao executor.")
    return redirect("control_plane:executor_detail", pk=executor.pk)


@require_POST
@login_required(login_url="/admin/login/")
def executor_capability_enable(request, pk):
    capability = get_object_or_404(
        ToolExecutorCapability.objects.select_related("executor", "executor__tenant", "tool_definition").filter(
            executor__tenant_id__in=_tenant_ids(request.user),
        ),
        pk=pk,
    )
    _require_manage(request.user, capability.executor.tenant)
    capability.is_enabled = True
    capability.save(update_fields=["is_enabled", "updated_at"])
    record_audit_event(action=ACTION_EXECUTOR_CAPABILITY_ENABLED, actor=request.user, tenant=capability.executor.tenant, obj=capability, request=request)
    messages.success(request, "Capability habilitada.")
    return redirect("control_plane:executor_detail", pk=capability.executor_id)


@require_POST
@login_required(login_url="/admin/login/")
def executor_capability_disable(request, pk):
    capability = get_object_or_404(
        ToolExecutorCapability.objects.select_related("executor", "executor__tenant", "tool_definition").filter(
            executor__tenant_id__in=_tenant_ids(request.user),
        ),
        pk=pk,
    )
    _require_manage(request.user, capability.executor.tenant)
    capability.is_enabled = False
    capability.save(update_fields=["is_enabled", "updated_at"])
    record_audit_event(action=ACTION_EXECUTOR_CAPABILITY_DISABLED, actor=request.user, tenant=capability.executor.tenant, obj=capability, request=request)
    messages.success(request, "Capability desabilitada.")
    return redirect("control_plane:executor_detail", pk=capability.executor_id)


@login_required(login_url="/admin/login/")
def executor_pairing_list(request):
    pairings = _pairing_queryset(request.user).order_by("-created_at")[:200]
    context = _base_context("executor_pairings")
    context.update({"pairings": pairings, "manageable_tenants": _manageable_tenants(request.user)})
    return render(request, "control_plane/executor_pairing_list.html", context)


@require_POST
@login_required(login_url="/admin/login/")
def executor_pairing_approve(request, pk):
    pairing = get_object_or_404(_pairing_queryset(request.user), pk=pk)
    tenant_id = request.POST.get("tenant_id") or pairing.tenant_id
    tenant = get_object_or_404(_accessible_tenants(request.user), pk=tenant_id)
    _require_manage(request.user, tenant)
    try:
        approve_pairing(pairing=pairing, tenant=tenant, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "Pairing aprovado. A credencial sera emitida somente no consumo do client.")
    return redirect("control_plane:executor_pairing_list")


@require_POST
@login_required(login_url="/admin/login/")
def executor_pairing_reject(request, pk):
    pairing = get_object_or_404(_pairing_queryset(request.user), pk=pk)
    if pairing.tenant_id is not None:
        _require_manage(request.user, pairing.tenant)
    elif not _manageable_tenants(request.user):
        raise PermissionDenied
    try:
        reject_pairing(pairing=pairing, actor=request.user, request=request)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, "Pairing rejeitado.")
    return redirect("control_plane:executor_pairing_list")


@require_POST
@login_required(login_url="/admin/login/")
def executor_credential_revoke(request, pk):
    credential = get_object_or_404(_credential_queryset(request.user), pk=pk)
    _require_manage(request.user, credential.executor.tenant)
    revoke_credential(credential=credential, actor=request.user, request=request)
    messages.success(request, "Credencial revogada.")
    return redirect("control_plane:executor_detail", pk=credential.executor_id)


@require_POST
@login_required(login_url="/admin/login/")
def executor_credential_rotate(request, pk):
    credential = get_object_or_404(_credential_queryset(request.user), pk=pk)
    _require_manage(request.user, credential.executor.tenant)
    issued = rotate_credential(credential=credential, actor=request.user, request=request)
    executor = get_object_or_404(_executor_queryset(request.user), pk=credential.executor_id)
    context = _base_context("executors")
    context.update(
        {
            "executor": executor,
            "status_label": _executor_status(executor),
            "credentials": executor.credentials.order_by("-created_at"),
            "capabilities": executor.capabilities.select_related("tool_definition").order_by("tool_definition__slug"),
            "can_manage": True,
            "one_time_secret": issued.secret,
        }
    )
    return render(request, "control_plane/executor_detail.html", context)
