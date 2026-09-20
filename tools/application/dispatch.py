from __future__ import annotations

from typing import Protocol

from tools.application.lifecycle import ACTION_EXECUTION_DISPATCHED, record_tool_execution_event, transition_execution
from tools.models import ToolExecution


class DelegatedToolDispatcherPort(Protocol):
    def dispatch(self, execution: ToolExecution, *, actor=None, request=None) -> ToolExecution:
        ...


class NoopDelegatedToolDispatcher:
    """Persistent no-op dispatcher used until external executors are integrated."""

    def dispatch(self, execution: ToolExecution, *, actor=None, request=None) -> ToolExecution:
        execution = transition_execution(execution, ToolExecution.Status.DISPATCHED)
        record_tool_execution_event(ACTION_EXECUTION_DISPATCHED, execution, actor=actor, request=request)
        return execution
