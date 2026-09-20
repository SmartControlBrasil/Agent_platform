from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from agents.application.installations import (
    disable_agent_installation,
    enable_agent_installation,
    install_agent as install_agent_service,
    update_installation_configuration,
)
from agents.models import AgentDefinition, AgentInstallation, AgentVersion
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
    context = _base_context("installations")
    context.update({"installation": installation, "form": form, "can_manage": can_manage})
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
