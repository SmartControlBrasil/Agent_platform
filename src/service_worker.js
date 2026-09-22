import { getChromeApi } from './chrome_api.js';
import { HEARTBEAT_ALARM, HEARTBEAT_PERIOD_MINUTES, JOB_POLL_ALARM, JOB_POLL_PERIOD_MINUTES, PAIRING_POLL_ALARM, PAIRING_POLL_PERIOD_MINUTES } from './constants.js';
import { heartbeat, pollAndRunJobs } from './executor_runtime.js';
import { pollPairing } from './pairing_flow.js';
import { getState } from './state.js';

const chromeApi = getChromeApi();

chromeApi.runtime.onInstalled.addListener(() => {
  chromeApi.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: HEARTBEAT_PERIOD_MINUTES });
  chromeApi.alarms.create(JOB_POLL_ALARM, { periodInMinutes: JOB_POLL_PERIOD_MINUTES });
  chromeApi.alarms.create(PAIRING_POLL_ALARM, { periodInMinutes: PAIRING_POLL_PERIOD_MINUTES });
});

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
