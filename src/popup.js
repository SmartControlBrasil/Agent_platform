import { getChromeApi } from './chrome_api.js';
import { CredentialStore } from './credential_store.js';
import { ExecutorState } from './constants.js';
import { startPairing } from './pairing_flow.js';
import { formatCapabilities } from './capabilities.js';
import { getState, resetOperationalState } from './state.js';

const chromeApi = getChromeApi();

async function render() {
  const state = await getState(chromeApi);
  setText('state', state.state);
  setText('server', state.platformBaseUrl || '-');
  setText('executor', state.executor?.name || state.executor?.public_id || '-');
  setText('heartbeat', state.lastHeartbeatAt || '-');
  setText('capabilities', formatCapabilities(state.executor?.capabilities));
  setText('last-execution', state.lastExecutionId ? `${state.lastExecutionId} (${state.lastExecutionStatus || '-'})` : '-');
  setText('error', state.lastErrorCode || '-');
  document.querySelector('#status-dot').className = `dot ${state.state.toLowerCase()}`;
  const pairingBox = document.querySelector('#pairing-box');
  if (state.pairing?.pairingCode) {
    pairingBox.classList.remove('hidden');
    setText('pairing-code', formatPairingCode(state.pairing.pairingCode));
  } else {
    pairingBox.classList.add('hidden');
  }
  document.querySelector('#pair').disabled = !state.platformBaseUrl || state.state === ExecutorState.PAIRING;
  document.querySelector('#open-platform').disabled = !state.platformBaseUrl;
}

function setText(id, value) {
  document.querySelector(`#${id}`).textContent = value;
}

function formatPairingCode(code) {
  return String(code || '').replace(/(.{4})/g, '$1-').replace(/-$/, '');
}

document.querySelector('#configure').addEventListener('click', () => chromeApi.runtime.openOptionsPage());
document.querySelector('#pair').addEventListener('click', async () => { await startPairing({ chromeApi }); await render(); });
document.querySelector('#disconnect').addEventListener('click', async () => { await new CredentialStore(chromeApi).clearCredential(); await resetOperationalState(chromeApi); await render(); });
document.querySelector('#open-platform').addEventListener('click', async () => {
  const state = await getState(chromeApi);
  if (state.platformBaseUrl) chromeApi.tabs?.create?.({ url: state.platformBaseUrl });
});

render();
