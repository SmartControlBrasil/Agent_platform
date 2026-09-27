import { FETCH_TIMEOUT_MS } from './constants.js';
import { joinUrl, normalizeBaseUrl } from './url.js';

export class AgentPlatformError extends Error {
  constructor(message, { status = 0, code = '' } = {}) {
    super(message);
    this.name = 'AgentPlatformError';
    this.status = status;
    this.code = code;
  }
}

export class AgentPlatformClient {
  constructor({ baseUrl, credential = '', fetchImpl = globalThis.fetch.bind(globalThis), timeoutMs = FETCH_TIMEOUT_MS }) {
    this.baseUrl = normalizeBaseUrl(baseUrl);
    this.credential = credential;
    this.fetchImpl = fetchImpl;
    this.timeoutMs = timeoutMs;
  }

  withCredential(credential) {
    return new AgentPlatformClient({
      baseUrl: this.baseUrl,
      credential,
      fetchImpl: this.fetchImpl,
      timeoutMs: this.timeoutMs,
    });
  }

  requestPairing({ executorType = 'BROWSER_EXTENSION', requestedName }) {
    return this.#request('/api/v1/executors/pairing/request/', {
      method: 'POST',
      body: { executor_type: executorType, requested_name: requestedName },
    });
  }

  getPairingStatus(id) {
    return this.#request(`/api/v1/executors/pairing/${encodeURIComponent(id)}/status/`);
  }

  consumePairing(id, pairingCode) {
    return this.#request(`/api/v1/executors/pairing/${encodeURIComponent(id)}/consume/`, {
      method: 'POST',
      body: { pairing_code: pairingCode },
    });
  }

  getExecutorMe() {
    return this.#request('/api/v1/executors/me/', { auth: true });
  }

  heartbeat() {
    return this.#request('/api/v1/executors/heartbeat/', { method: 'POST', auth: true });
  }

  listToolExecutions() {
    return this.#request('/api/v1/executors/tool-executions/', { auth: true });
  }

  claimToolExecution(id) {
    return this.#request(`/api/v1/executors/tool-executions/${encodeURIComponent(id)}/claim/`, { method: 'POST', auth: true });
  }

  completeToolExecution(id, result) {
    return this.#request(`/api/v1/executors/tool-executions/${encodeURIComponent(id)}/complete/`, {
      method: 'POST',
      auth: true,
      body: { result },
    });
  }

  failToolExecution(id, error) {
    return this.#request(`/api/v1/executors/tool-executions/${encodeURIComponent(id)}/fail/`, {
      method: 'POST',
      auth: true,
      body: {
        error_code: error?.error_code || error?.code || 'handler_error',
        error_message: error?.error_message || error?.message || '',
      },
    });
  }

  async #request(path, { method = 'GET', body = undefined, auth = false } = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    const headers = { Accept: 'application/json' };
    const init = { method, headers, signal: controller.signal };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    if (auth) {
      if (!this.credential) throw new AgentPlatformError('credential_required', { code: 'credential_required' });
      headers.Authorization = `AgentExecutor ${this.credential}`;
    }
    try {
      const response = await this.fetchImpl(joinUrl(this.baseUrl, path), init);
      const payload = await parseJsonResponse(response);
      if (!response.ok) {
        throw new AgentPlatformError(payload?.error || `http_${response.status}`, {
          status: response.status,
          code: payload?.error || '',
        });
      }
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') {
        throw new AgentPlatformError('request_timeout', { code: 'request_timeout' });
      }
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }
}

async function parseJsonResponse(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new AgentPlatformError('invalid_json_response', { status: response.status, code: 'invalid_json_response' });
  }
}
