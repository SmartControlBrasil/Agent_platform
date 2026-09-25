from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from agents.models import AgentDefinition, AgentInstallation, AgentVersion
from projects.models import Project
from tenants.models import Tenant
from tools.models import AgentToolBinding, ToolDefinition, ToolExecutor, ToolExecutorCapability


class Command(BaseCommand):
    help = "Provisiona workspace operacional idempotente para prospecção em um tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant-slug", default="smart-control-brasil")
        parser.add_argument("--project-slug", default="prospeccao-comercial")
        parser.add_argument("--project-name", default="Prospecção Comercial")
        parser.add_argument("--installation-name", default="Prospecting Comercial")
        parser.add_argument("--ensure-executor", action="store_true")
        parser.add_argument("--executor-name", default="SCB Chrome Executor Operacional")
        parser.add_argument("--executor-public-id", default="")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if options["dry_run"] and options["apply"]:
            raise CommandError("Use apenas um modo: --dry-run ou --apply.")
        if not options["dry_run"] and not options["apply"]:
            raise CommandError("Modo explícito obrigatório: use --dry-run para simular ou --apply para gravar.")

        tenant = Tenant.objects.filter(slug=options["tenant_slug"], is_active=True).first()
        if tenant is None:
            raise CommandError(f"Tenant ativo não encontrado para slug={options['tenant_slug']}.")

        definition = AgentDefinition.objects.filter(slug="prospecting", is_active=True).first()
        if definition is None:
            raise CommandError("AgentDefinition ativo de slug=prospecting não encontrado.")
        version = (
            AgentVersion.objects.filter(agent_definition=definition, status=AgentVersion.Status.ACTIVE)
            .order_by("-created_at")
            .first()
        )
        if version is None:
            raise CommandError("AgentVersion ativa para prospecting não encontrada.")

        plan_tool = ToolDefinition.objects.filter(slug="prospecting.build_search_plan", is_active=True).first()
        maps_tool = ToolDefinition.objects.filter(slug="prospecting.search_google_maps", is_active=True).first()
        if plan_tool is None or maps_tool is None:
            raise CommandError("ToolDefinitions necessárias não encontradas/ativas.")

        if options["dry_run"]:
            summary = self._simulate(
                tenant=tenant,
                definition=definition,
                version=version,
                plan_tool=plan_tool,
                maps_tool=maps_tool,
                options=options,
            )
        else:
            summary = self._apply(
                tenant=tenant,
                definition=definition,
                version=version,
                plan_tool=plan_tool,
                maps_tool=maps_tool,
                options=options,
            )

        mode_label = "DRY RUN - nenhuma alteração gravada" if options["dry_run"] else "Provisionamento aplicado"
        self.stdout.write(self.style.SUCCESS(mode_label))
        self.stdout.write(f"Tenant: {tenant.slug} ({tenant.name})")
        self.stdout.write(f"Project: {summary['project_slug']} ({'criado' if summary['project_created'] else 'reutilizado'})")
        self.stdout.write(
            "Installation: "
            f"{summary['installation_id']} ({'criada' if summary['installation_created'] else 'reutilizada'})"
        )
        self.stdout.write(
            "Bindings: "
            f"plan={summary['plan_binding_state']} maps={summary['maps_binding_state']}"
        )
        self.stdout.write(
            "Executor: "
            f"{summary['executor_state']}"
        )
        self.stdout.write(
            "Ready: "
            f"{'yes' if summary['ready'] else 'no'}"
        )

    def _simulate(self, *, tenant, definition, version, plan_tool, maps_tool, options):
        project = Project.objects.filter(tenant=tenant, slug=options["project_slug"]).first()
        installation = self._select_installation(tenant=tenant, project=project, definition=definition)
        plan_binding = None
        maps_binding = None
        if installation is not None:
            plan_binding = AgentToolBinding.objects.filter(agent_installation=installation, tool_definition=plan_tool).first()
            maps_binding = AgentToolBinding.objects.filter(agent_installation=installation, tool_definition=maps_tool).first()
        executor = self._select_executor(tenant=tenant, maps_tool=maps_tool)
        if executor is None and options["ensure_executor"]:
            executor = ToolExecutor.objects.filter(
                tenant=tenant,
                executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
                name=options["executor_name"],
            ).first()
        capability = None
        if executor is not None:
            capability = ToolExecutorCapability.objects.filter(executor=executor, tool_definition=maps_tool).first()
        return {
            "project_slug": options["project_slug"],
            "project_created": project is None,
            "installation_id": str(installation.id) if installation else "<novo>",
            "installation_created": installation is None,
            "plan_binding_state": "novo" if plan_binding is None else ("reativado" if not plan_binding.is_enabled else "ok"),
            "maps_binding_state": "novo" if maps_binding is None else ("reativado" if not maps_binding.is_enabled else "ok"),
            "executor_state": self._executor_state(executor=executor, capability=capability, ensure_requested=options["ensure_executor"]),
            "ready": bool(
                project is not None
                and project.is_active
                and installation is not None
                and installation.is_enabled
                and plan_binding is not None
                and plan_binding.is_enabled
                and maps_binding is not None
                and maps_binding.is_enabled
                and executor is not None
                and executor.is_active
                and capability is not None
                and capability.is_enabled
            ),
        }

    def _apply(self, *, tenant, definition, version, plan_tool, maps_tool, options):
        with transaction.atomic():
            project, project_created = Project.objects.get_or_create(
                tenant=tenant,
                slug=options["project_slug"],
                defaults={"name": options["project_name"], "is_active": True},
            )
            project_updated_fields = []
            if not project.is_active:
                project.is_active = True
                project_updated_fields.append("is_active")
            if project.name != options["project_name"]:
                project.name = options["project_name"]
                project_updated_fields.append("name")
            if project_updated_fields:
                project.save(update_fields=[*project_updated_fields, "updated_at"])

            installation = self._select_installation(tenant=tenant, project=project, definition=definition)
            installation_created = installation is None
            if installation is None:
                installation = AgentInstallation.objects.create(
                    tenant=tenant,
                    project=project,
                    agent_definition=definition,
                    agent_version=version,
                    name=options["installation_name"],
                    is_enabled=True,
                    configuration={},
                )
            else:
                install_updates = []
                if not installation.is_enabled:
                    installation.is_enabled = True
                    install_updates.append("is_enabled")
                if installation.agent_version_id != version.id:
                    installation.agent_version = version
                    install_updates.append("agent_version")
                if install_updates:
                    installation.save(update_fields=[*install_updates, "updated_at"])

            plan_binding, plan_created = AgentToolBinding.objects.get_or_create(
                agent_installation=installation,
                tool_definition=plan_tool,
                defaults={
                    "tenant": tenant,
                    "project": project,
                    "is_enabled": True,
                    "configuration": {},
                },
            )
            plan_was_disabled = (not plan_created) and (not plan_binding.is_enabled)
            if plan_was_disabled:
                plan_binding.is_enabled = True
                plan_binding.save(update_fields=["is_enabled", "updated_at"])

            maps_binding, maps_created = AgentToolBinding.objects.get_or_create(
                agent_installation=installation,
                tool_definition=maps_tool,
                defaults={
                    "tenant": tenant,
                    "project": project,
                    "is_enabled": True,
                    "configuration": {},
                },
            )
            maps_was_disabled = (not maps_created) and (not maps_binding.is_enabled)
            if maps_was_disabled:
                maps_binding.is_enabled = True
                maps_binding.save(update_fields=["is_enabled", "updated_at"])

            executor = self._select_executor(tenant=tenant, maps_tool=maps_tool)
            capability = None
            executor_state = "não provisionado"
            if executor is None and options["ensure_executor"]:
                executor = ToolExecutor.objects.filter(
                    tenant=tenant,
                    executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
                    name=options["executor_name"],
                ).first()
                if executor is None:
                    public_id = (options["executor_public_id"] or "").strip() or f"exec_{uuid.uuid4().hex[:24]}"
                    executor = ToolExecutor.objects.create(
                        tenant=tenant,
                        name=options["executor_name"],
                        executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
                        public_id=public_id,
                        is_active=True,
                    )
            if executor is not None:
                if not executor.is_active:
                    executor.is_active = True
                    executor.save(update_fields=["is_active", "updated_at"])
                capability, _ = ToolExecutorCapability.objects.get_or_create(
                    executor=executor,
                    tool_definition=maps_tool,
                    defaults={"is_enabled": True},
                )
                if not capability.is_enabled:
                    capability.is_enabled = True
                    capability.save(update_fields=["is_enabled", "updated_at"])
                executor_state = f"{executor.name} ({executor.public_id})"

            ready = bool(
                project.is_active
                and installation.is_enabled
                and plan_binding.is_enabled
                and maps_binding.is_enabled
                and executor is not None
                and executor.is_active
                and capability is not None
                and capability.is_enabled
            )
            return {
                "project_slug": project.slug,
                "project_created": project_created,
                "installation_id": str(installation.id),
                "installation_created": installation_created,
                "plan_binding_state": "criado" if plan_created else ("reativado" if plan_was_disabled else "ok"),
                "maps_binding_state": "criado" if maps_created else ("reativado" if maps_was_disabled else "ok"),
                "executor_state": executor_state,
                "ready": ready,
            }

    @staticmethod
    def _select_installation(*, tenant, project, definition):
        if project is None:
            return None
        return (
            AgentInstallation.objects.filter(tenant=tenant, project=project, agent_definition=definition)
            .order_by("-is_enabled", "created_at")
            .first()
        )

    @staticmethod
    def _select_executor(*, tenant, maps_tool):
        return (
            ToolExecutor.objects.filter(
                tenant=tenant,
                executor_type=ToolExecutor.ExecutorType.BROWSER_EXTENSION,
                capabilities__tool_definition=maps_tool,
            )
            .distinct()
            .order_by("-is_active", "-last_seen_at", "created_at")
            .first()
        )

    @staticmethod
    def _executor_state(*, executor, capability, ensure_requested):
        if executor is None:
            return "não provisionado" if not ensure_requested else "será criado"
        if capability is None:
            return f"{executor.name} sem capability maps"
        if not capability.is_enabled:
            return f"{executor.name} capability maps desabilitada"
        return f"{executor.name} ({executor.public_id})"
