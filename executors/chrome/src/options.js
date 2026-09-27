import { getChromeApi } from './chrome_api.js';
import { DEFAULT_REQUESTED_NAME, ExecutorState } from './constants.js';
import { getState, setState } from './state.js';
import { normalizeBaseUrl, originPermissionPattern } from './url.js';

const chromeApi = getChromeApi();

async function load() {
  const state = await getState(chromeApi);
  document.querySelector('#platform-url').value = state.platformBaseUrl || '';
  document.querySelector('#requested-name').value = state.requestedName || DEFAULT_REQUESTED_NAME;
}

document.querySelector('#options-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const message = document.querySelector('#message');
  try {
    const platformBaseUrl = normalizeBaseUrl(document.querySelector('#platform-url').value);
    const requestedName = document.querySelector('#requested-name').value.trim() || DEFAULT_REQUESTED_NAME;
    await requestOriginPermission(platformBaseUrl);
    await setState({ platformBaseUrl, requestedName, state: ExecutorState.UNPAIRED }, chromeApi);
    message.textContent = 'Configuracao salva.';
  } catch (error) {
    message.textContent = 'URL invalida. Use http ou https.';
  }
});

load();

async function requestOriginPermission(platformBaseUrl) {
  if (!chromeApi.permissions?.request) return true;
  const granted = await chromeApi.permissions.request({ origins: [originPermissionPattern(platformBaseUrl)] });
  if (!granted) throw new Error('origin_permission_denied');
  return true;
}
