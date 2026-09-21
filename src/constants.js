export const ExecutorState = Object.freeze({
  UNCONFIGURED: 'UNCONFIGURED',
  UNPAIRED: 'UNPAIRED',
  PAIRING: 'PAIRING',
  PAIRED: 'PAIRED',
  ONLINE: 'ONLINE',
  ERROR: 'ERROR',
  REVOKED: 'REVOKED',
});

export const ToolSlugs = Object.freeze({
  EXTERNAL_SEARCH_PROBE: 'prospecting.external_search_probe',
});

export const DEFAULT_REQUESTED_NAME = 'Chrome Executor';
export const HEARTBEAT_ALARM = 'agentExecutorHeartbeat';
export const JOB_POLL_ALARM = 'agentExecutorJobPoll';
export const PAIRING_POLL_ALARM = 'agentExecutorPairingPoll';
export const HEARTBEAT_PERIOD_MINUTES = 1;
export const JOB_POLL_PERIOD_MINUTES = 1;
export const PAIRING_POLL_PERIOD_MINUTES = 0.1;
export const FETCH_TIMEOUT_MS = 15000;
