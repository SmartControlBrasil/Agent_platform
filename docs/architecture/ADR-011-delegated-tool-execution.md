# ADR-011: Delegated Tool Execution Gateway

Status: Accepted

## Context

Agent tools started as local capabilities executed inside Agent Platform. Some future capabilities need to run outside the platform, for example in `smart_sales`, a Chrome extension, or a specialized worker. The agent runtime must not know which execution mechanism is used. It calls `execute_tool(...)`; the application service chooses the correct path.

## Decision

`ToolDefinition` now has an `execution_mode`:

- `LOCAL`: executed by `ToolRuntimeRegistry` inside Agent Platform.
- `DELEGATED`: persisted as a `ToolExecution` and dispatched for a future external executor.

`prospecting.build_search_plan` remains `LOCAL`. `prospecting.external_search_probe` is a delegated laboratory tool used only to prove the lifecycle. It performs no search and has no local runtime handler.

## ToolExecution Lifecycle

`ToolExecution` stores tenant, project, installation, binding, definition, mode, status, sanitized request/result payloads, optional executor, idempotency key, timestamps, and optional `expires_at`. Valid transitions are controlled by application services:

- `PENDING -> RUNNING -> SUCCEEDED / FAILED` for local tools.
- `PENDING -> DISPATCHED -> RUNNING -> SUCCEEDED / FAILED` for delegated tools.
- `CANCELLED` and `EXPIRED` are terminal states.

Terminal states cannot return to `RUNNING`. Status changes should go through lifecycle services rather than ad hoc updates.

## Idempotency

Idempotency is scoped by `tenant + agent_installation + tool_definition + idempotency_key`. The platform never treats a caller-provided key as globally authoritative. A duplicate key in the same execution context returns the existing execution result/state rather than creating another execution. Empty keys do not participate in the uniqueness constraint.

## Delegated Dispatch

A `DelegatedToolDispatcherPort` defines the dispatch boundary. The current implementation is `NoopDelegatedToolDispatcher`, which persists the execution and marks it `DISPATCHED` without calling external services. This keeps the protocol testable without prematurely implementing an executor.

## ToolExecutor And Capabilities

`ToolExecutor` represents a future authorized executor such as a browser extension, `smart_sales`, or a worker. It stores non-secret identity metadata only. `ToolExecutorCapability` explicitly authorizes an executor for a tool. Executors cannot claim arbitrary executions.

## Claim And Completion

`claim_tool_execution` validates that the executor is active, tenant-compatible, capability-enabled, and that the execution is delegated, dispatched, unexpired, and unclaimed. It uses transaction locking where available and moves `DISPATCHED -> RUNNING`.

`complete_tool_execution` and `fail_tool_execution` validate claim ownership. The wrong executor cannot complete or fail another executor's work. Error text is sanitized before persistence.

## API Security

Initial polling endpoints exist under `/api/v1/tool-executions/` and require the platform's existing authenticated user/session model plus tenant scoping. Because executor authentication is not final, the endpoints use `executor_public_id` only as executor selection inside an already authenticated, tenant-scoped request. No hardcoded executor secret is introduced. Future phases should replace this with a proper executor credential or signed token backed by a secret vault.

## Tenant Isolation

Control Plane and API queries are tenant-scoped. Tenant-scoped executors can only claim executions in their tenant. Platform-level executors are represented by `tenant = null`, but still require explicit capabilities.

## Expiration And Retry

`expires_at` prepares timeout handling. Claims after expiration move the execution to `EXPIRED` and fail. No scheduler or automatic retry policy is implemented yet. A later phase should define retry count, retry windows, and scheduler ownership before adding columns or automation.

## Future Chrome Executor

A Chrome Extension can become a `ToolExecutor` for a future `prospecting.search_google_maps` tool. It would claim a dispatched execution, perform browser-side Google Maps work, then complete the execution with results. This ADR does not implement Google Maps or browser execution.

## Future smart_sales Role

`smart_sales` may be both a client of Agent Platform and a `ToolExecutor` for selected capabilities. These roles must remain explicit and separate: client access does not imply executor capability, and executor capability does not imply general client authority.
