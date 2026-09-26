import { getChromeApi } from './chrome_api.js';
import { HEARTBEAT_ALARM, HEARTBEAT_PERIOD_MINUTES, JOB_POLL_ALARM, JOB_POLL_PERIOD_MINUTES, PAIRING_POLL_ALARM, PAIRING_POLL_PERIOD_MINUTES } from './constants.js';
import { heartbeat, pollAndRunJobs } from './executor_runtime.js';
import { pollPairing } from './pairing_flow.js';
import { getState } from './state.js';
import { CredentialStore } from './credential_store.js';

const chromeApi = getChromeApi();

function registerAlarms() {
  chromeApi.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: HEARTBEAT_PERIOD_MINUTES });
  chromeApi.alarms.create(JOB_POLL_ALARM, { periodInMinutes: JOB_POLL_PERIOD_MINUTES });
  chromeApi.alarms.create(PAIRING_POLL_ALARM, { periodInMinutes: PAIRING_POLL_PERIOD_MINUTES });
}

async function bootstrapOperationalLoops() {
  const state = await getState(chromeApi);
  if (!state.platformBaseUrl) return;
  const credential = await new CredentialStore(chromeApi).getCredential();
  if (!credential) return;
  const freshState = await getState(chromeApi);
  await heartbeat({ state: freshState, chromeApi });
  await pollAndRunJobs({ state: await getState(chromeApi), chromeApi });
}

function scheduleBootstrap() {
  bootstrapOperationalLoops().catch(() => {});
}

chromeApi.runtime.onInstalled.addListener(() => {
  registerAlarms();
  scheduleBootstrap();
});

chromeApi.runtime.onStartup.addListener(() => {
  registerAlarms();
  scheduleBootstrap();
});

registerAlarms();
scheduleBootstrap();

chromeApi.alarms.onAlarm.addListener(async (alarm) => {
  const state = await getState(chromeApi);
  if (alarm.name === PAIRING_POLL_ALARM) await pollPairing({ chromeApi });
  if (alarm.name === HEARTBEAT_ALARM) await heartbeat({ state, chromeApi });
  if (alarm.name === JOB_POLL_ALARM) await pollAndRunJobs({ state, chromeApi });
});


chromeApi.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== 'AGENT_EXECUTOR_DEV_POLL_NOW') return false;
  if (sender.id !== chromeApi.runtime.id) {
    sendResponse({ ok: false, code: 'unsupported_message' });
    return false;
  }
  getState(chromeApi)
    .then((state) => pollAndRunJobs({ state, chromeApi }))
    .then((handled) => sendResponse({ ok: true, handled }))
    .catch(() => sendResponse({ ok: false, code: 'handler_error' }));
  return true;
});
