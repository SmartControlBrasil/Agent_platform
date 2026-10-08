import { AgentPlatformClient, AgentPlatformError } from './api_client.js';
import { CredentialStore } from './credential_store.js';
import { ExecutorState } from './constants.js';
import { normalizeQueueResponse } from './response_validation.js';
import { setState } from './state.js';
import { safeErrorCode } from './safe_log.js';
import { ToolDispatcher, defaultRegistry } from './tool_dispatcher.js';

export async function createClientFromState(state, chromeApi, fetchImpl = globalThis.fetch.bind(globalThis)) {
  const credential = await new CredentialStore(chromeApi).getCredential();
  return new AgentPlatformClient({ baseUrl: state.platformBaseUrl, credential, fetchImpl });
}

export async function heartbeat({ state, chromeApi, fetchImpl }) {
  if (!state.platformBaseUrl) return null;
  const client = await createClientFromState(state, chromeApi, fetchImpl);
  try {
    const payload = await client.heartbeat();
    await setState({ state: ExecutorState.ONLINE, executor: payload.executor, lastHeartbeatAt: new Date().toISOString(), lastErrorCode: '' }, chromeApi);
    return payload;
  } catch (error) {
    await handleAuthOrError(error, chromeApi);
    return null;
  }
}

export async function pollAndRunJobs({ state, chromeApi, fetchImpl, dispatcher = null }) {
  if (!state.platformBaseUrl) return [];
  const client = await createClientFromState(state, chromeApi, fetchImpl);
  try {
    const queue = normalizeQueueResponse(await client.listToolExecutions());
    await setState({ lastPollAt: new Date().toISOString(), lastErrorCode: '' }, chromeApi);
    const handled = [];
    for (const execution of queue) {
      const result = await claimAndExecute({ client, execution, dispatcher, chromeApi });
      handled.push(result);
    }
    return handled;
  } catch (error) {
    await handleAuthOrError(error, chromeApi);
    return [];
  }
}

export async function claimAndExecute({ client, execution, dispatcher, chromeApi }) {
  let claimed;
  try {
    claimed = await client.claimToolExecution(execution.id);
  } catch (error) {
    await setState({ lastExecutionId: execution.id, lastExecutionStatus: 'claim_rejected', lastErrorCode: safeErrorCode(error) }, chromeApi);
    return { id: execution.id, status: 'claim_rejected' };
  }

  try {
    const progressReporter = createProgressReporter(client, execution.id);
    const activeDispatcher = dispatcher || new ToolDispatcher(defaultRegistryWithChrome(chromeApi, progressReporter));
    await progressReporter({ stage: 'executor_started', processed_results: 0 });
    const result = await activeDispatcher.execute(execution);
    const completed = await client.completeToolExecution(execution.id, result);
    await setState({ lastExecutionId: execution.id, lastExecutionStatus: completed.status || 'SUCCEEDED', lastErrorCode: '' }, chromeApi);
    return { id: execution.id, status: 'completed' };
  } catch (error) {
    const code = safeErrorCode(error);
    logExecutionFailure(execution.id, code, error?.diagnostics);
    await client.failToolExecution(execution.id, { error_code: code, error_message: 'Tool execution failed.', diagnostics: error?.diagnostics });
    await setState({ lastExecutionId: execution.id, lastExecutionStatus: 'FAILED', lastErrorCode: code }, chromeApi);
    return { id: execution.id, status: 'failed' };
  }
}

export async function handleAuthOrError(error, chromeApi) {
  if (error instanceof AgentPlatformError && [401, 403].includes(error.status)) {
    await setState({ state: ExecutorState.REVOKED, lastErrorCode: error.code || 'auth_failed' }, chromeApi);
  } else {
    await setState({ state: ExecutorState.ERROR, lastErrorCode: safeErrorCode(error) }, chromeApi);
  }
}

function defaultRegistryWithChrome(chromeApi, progressReporter = null) {
  return defaultRegistry({ chromeApi, progressReporter });
}

function createProgressReporter(client, executionId) {
  return async (progress = {}) => {
    try {
      await client.progressToolExecution(executionId, {
        ...progress,
        last_activity_at: new Date().toISOString(),
      });
    } catch (error) {
      console.warn('executor.progress_failed', {
        execution_id: String(executionId || '').slice(0, 80),
        stage: safeLogText(progress?.stage, 80),
        error_code: safeErrorCode(error),
        http_status: Number.isFinite(error?.status) ? error.status : undefined,
        message: safeLogText(error?.message, 160),
      });
    }
  };
}


function logExecutionFailure(executionId, errorCode, diagnostics = null) {
  const safeDiagnostics = diagnostics && typeof diagnostics === 'object' ? diagnostics : {};
  console.warn('executor.execution_failed', {
    execution_id: String(executionId || '').slice(0, 80),
    error_code: safeLogText(errorCode, 80),
    has_diagnostics: Object.keys(safeDiagnostics).length > 0,
    diagnostics_stage: safeLogText(safeDiagnostics.stage, 80),
    diagnostics_feed_found: safeDiagnostics.feed_found ?? safeDiagnostics.hasResultsFeed,
    diagnostics_article_count: safeNumber(safeDiagnostics.article_count ?? safeDiagnostics.articleCount),
    diagnostics_place_anchor_count: safeNumber(safeDiagnostics.place_anchor_count ?? safeDiagnostics.placeAnchorCount),
  });
}

function safeLogText(value, limit) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  if (/aep_[A-Za-z0-9_\-]+|authorization|cookie|credential|secret|token/i.test(text)) return '[sanitized]';
  return text.slice(0, limit);
}

function safeNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : undefined;
}
