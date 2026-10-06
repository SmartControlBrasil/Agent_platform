from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from prospecting.application.search_runs import synchronize_search_run
from prospecting.models import SearchRunExecutionAttempt
from tenants.models import Tenant
from tools.application.lifecycle import TERMINAL_STATUSES, ToolExecutionLifecycleError, expire_tool_execution
from tools.models import ToolExecution

EXPIRABLE_STATUSES = (
    ToolExecution.Status.PENDING,
    ToolExecution.Status.DISPATCHED,
    ToolExecution.Status.RUNNING,
)


class Command(BaseCommand):
    help = "Expira ToolExecutions com expires_at vencido e sincroniza SearchRuns vinculadas."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", dest="tenant_slug", help="Limita o sweep a um tenant pelo slug.")
        parser.add_argument("--dry-run", action="store_true", help="Mostra contadores sem alterar execuções.")
        parser.add_argument("--limit", type=int, default=500, help="Máximo de execuções a processar nesta rodada.")

    def handle(self, *args, **options):
        limit = max(1, int(options.get("limit") or 500))
        tenant_slug = (options.get("tenant_slug") or "").strip()
        dry_run = bool(options.get("dry_run"))
        tenant = None
        if tenant_slug:
            try:
                tenant = Tenant.objects.get(slug=tenant_slug)
            except Tenant.DoesNotExist as exc:
                raise CommandError(f"Tenant não encontrado: {tenant_slug}") from exc

        queryset = (
            ToolExecution.objects.select_related("tenant", "tool_definition")
            .filter(
                status__in=EXPIRABLE_STATUSES,
                expires_at__isnull=False,
                expires_at__lte=timezone.now(),
            )
            .exclude(status__in=TERMINAL_STATUSES)
            .order_by("expires_at", "created_at")
        )
        if tenant is not None:
            queryset = queryset.filter(tenant=tenant)

        execution_ids = list(queryset.values_list("id", flat=True)[:limit])
        counters = Counter(scanned=len(execution_ids), expired=0, synced_search_runs=0, errors=0)
        by_tenant: Counter[str] = Counter()
        if dry_run:
            for item in queryset.filter(id__in=execution_ids).values_list("tenant__slug", flat=True):
                by_tenant[item or ""] += 1
            self._write_summary(counters=counters, by_tenant=by_tenant, dry_run=True)
            return

        for execution_id in execution_ids:
            try:
                with transaction.atomic():
                    execution = (
                        ToolExecution.objects.select_for_update()
                        .select_related("tenant", "tool_definition")
                        .get(pk=execution_id)
                    )
                    if (
                        execution.status not in EXPIRABLE_STATUSES
                        or execution.expires_at is None
                        or execution.expires_at > timezone.now()
                    ):
                        continue
                    expire_tool_execution(execution)
                    counters["expired"] += 1
                    by_tenant[execution.tenant.slug] += 1
                attempt = (
                    SearchRunExecutionAttempt.objects.select_related("search_run")
                    .filter(tool_execution_id=execution_id)
                    .first()
                )
                if attempt is not None:
                    synchronize_search_run(search_run=attempt.search_run)
                    counters["synced_search_runs"] += 1
            except (ToolExecution.DoesNotExist, ToolExecutionLifecycleError) as exc:
                counters["errors"] += 1
                self.stderr.write(f"execution={execution_id} error={exc}")

        self._write_summary(counters=counters, by_tenant=by_tenant, dry_run=False)

    def _write_summary(self, *, counters: Counter, by_tenant: Counter[str], dry_run: bool) -> None:
        mode = "dry-run" if dry_run else "apply"
        self.stdout.write(
            "tool_execution_expiry "
            f"mode={mode} scanned={counters['scanned']} expired={counters['expired']} "
            f"synced_search_runs={counters['synced_search_runs']} errors={counters['errors']}"
        )
        for tenant_slug, count in sorted(by_tenant.items()):
            self.stdout.write(f"tenant={tenant_slug} expired={count if not dry_run else 0} candidates={count}")
