import { DEFAULT_REQUESTED_NAME, ExecutorState } from './constants.js';
import { normalizeBaseUrl } from './url.js';
import { storageGet, storageSet } from './chrome_api.js';

const STATE_KEY = 'agentExecutorState';
const DEFAULT_STATE = Object.freeze({
  state: ExecutorState.UNCONFIGURED,
  platformBaseUrl: '',
  requestedName: DEFAULT_REQUESTED_NAME,
  pairing: null,
  executor: null,
  lastHeartbeatAt: '',
  lastPollAt: '',
  lastExecutionId: '',
  lastExecutionStatus: '',
  lastErrorCode: '',
});

export function defaultState() {
  return { ...DEFAULT_STATE };
}

export function deriveState(current) {
  if (!current.platformBaseUrl) return ExecutorState.UNCONFIGURED;
  if (current.state === ExecutorState.REVOKED) return ExecutorState.REVOKED;
  if (current.state === ExecutorState.ERROR) return ExecutorState.ERROR;
  if (current.pairing) return ExecutorState.PAIRING;
  if (current.executor && current.lastHeartbeatAt) return ExecutorState.ONLINE;
  if (current.executor) return ExecutorState.PAIRED;
  return ExecutorState.UNPAIRED;
}

export function sanitizeState(input = {}) {
  const next = { ...DEFAULT_STATE, ...input };
  if (next.platformBaseUrl) {
    next.platformBaseUrl = normalizeBaseUrl(next.platformBaseUrl);
  }
  next.requestedName = String(next.requestedName || DEFAULT_REQUESTED_NAME).trim() || DEFAULT_REQUESTED_NAME;
  next.state = Object.values(ExecutorState).includes(next.state) ? next.state : deriveState(next);
  return next;
}

export async function getState(chromeApi) {
  const data = await storageGet([STATE_KEY], chromeApi);
  return sanitizeState(data[STATE_KEY] || DEFAULT_STATE);
}

export async function setState(patch, chromeApi) {
  const current = await getState(chromeApi);
  const next = sanitizeState({ ...current, ...patch });
  if (!patch.state) {
    next.state = deriveState(next);
  }
  await storageSet({ [STATE_KEY]: next }, chromeApi);
  return next;
}

export async function resetOperationalState(chromeApi) {
  return setState({
    state: ExecutorState.UNPAIRED,
    pairing: null,
    executor: null,
    lastHeartbeatAt: '',
    lastPollAt: '',
    lastExecutionId: '',
    lastExecutionStatus: '',
    lastErrorCode: '',
  }, chromeApi);
}
