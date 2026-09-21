import { AgentPlatformClient } from './api_client.js';
import { CredentialStore } from './credential_store.js';
import { DEFAULT_REQUESTED_NAME, ExecutorState } from './constants.js';
import { getState, setState } from './state.js';

export async function startPairing({ chromeApi, fetchImpl = globalThis.fetch }) {
  const state = await getState(chromeApi);
  const client = new AgentPlatformClient({ baseUrl: state.platformBaseUrl, fetchImpl });
  const pairing = await client.requestPairing({ requestedName: state.requestedName || DEFAULT_REQUESTED_NAME });
  await setState({
    state: ExecutorState.PAIRING,
    pairing: {
      id: pairing.id,
      pairingCode: pairing.pairing_code,
      expiresAt: pairing.expires_at,
      status: pairing.status,
      consumed: false,
    },
    lastErrorCode: '',
  }, chromeApi);
  return pairing;
}

export async function pollPairing({ chromeApi, fetchImpl = globalThis.fetch }) {
  const state = await getState(chromeApi);
  if (!state.platformBaseUrl || !state.pairing?.id) return null;
  const client = new AgentPlatformClient({ baseUrl: state.platformBaseUrl, fetchImpl });
  const status = await client.getPairingStatus(state.pairing.id);
  if (status.status === 'APPROVED' && !state.pairing.consumed) {
    return consumeApprovedPairing({ chromeApi, fetchImpl });
  }
  if (['REJECTED', 'EXPIRED'].includes(status.status)) {
    await setState({ state: status.status === 'REJECTED' ? ExecutorState.ERROR : ExecutorState.UNPAIRED, pairing: null, lastErrorCode: status.status.toLowerCase() }, chromeApi);
    return status;
  }
  await setState({ pairing: { ...state.pairing, status: status.status }, state: ExecutorState.PAIRING }, chromeApi);
  return status;
}

export async function consumeApprovedPairing({ chromeApi, fetchImpl = globalThis.fetch }) {
  const state = await getState(chromeApi);
  if (!state.pairing?.id || !state.pairing?.pairingCode) throw new Error('pairing_not_ready');
  if (state.pairing.consumed) throw new Error('pairing_already_consumed');
  await setState({ pairing: { ...state.pairing, consumed: true } }, chromeApi);
  const client = new AgentPlatformClient({ baseUrl: state.platformBaseUrl, fetchImpl });
  const consumed = await client.consumePairing(state.pairing.id, state.pairing.pairingCode);
  await new CredentialStore(chromeApi).setCredential(consumed.credential);
  await setState({ state: ExecutorState.PAIRED, pairing: null, executor: consumed.executor, lastErrorCode: '' }, chromeApi);
  return consumed;
}
