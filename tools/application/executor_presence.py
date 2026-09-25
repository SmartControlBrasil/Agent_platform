from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone


@dataclass(frozen=True)
class ExecutorPresence:
    label: str
    status: str
    is_online: bool


def executor_presence(executor) -> ExecutorPresence:
    if executor is None:
        return ExecutorPresence(label="Aguardando executor", status="offline", is_online=False)
    if not executor.is_active:
        return ExecutorPresence(label="Inativo", status="inactive", is_online=False)
    if executor.last_seen_at is None:
        return ExecutorPresence(label="Offline", status="offline", is_online=False)
    age = timezone.now() - executor.last_seen_at
    online_minutes = max(int(getattr(settings, "TOOL_EXECUTOR_ONLINE_THRESHOLD_MINUTES", 5)), 1)
    recent_minutes = max(int(getattr(settings, "TOOL_EXECUTOR_RECENT_THRESHOLD_MINUTES", 30)), online_minutes)
    if age <= timezone.timedelta(minutes=online_minutes):
        return ExecutorPresence(label="Online", status="online", is_online=True)
    if age <= timezone.timedelta(minutes=recent_minutes):
        return ExecutorPresence(label="Offline / heartbeat recente", status="recent", is_online=False)
    return ExecutorPresence(label="Offline", status="offline", is_online=False)
