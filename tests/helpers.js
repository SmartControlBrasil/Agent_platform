export function createChromeMock(initial = {}) {
  const store = { ...initial };
  const listeners = [];
  return {
    __store: store,
    storage: {
      local: {
        async get(keys) {
          if (Array.isArray(keys)) return Object.fromEntries(keys.map((key) => [key, store[key]]));
          if (typeof keys === 'string') return { [keys]: store[keys] };
          return { ...store };
        },
        async set(values) {
          Object.assign(store, values);
        },
        async remove(keys) {
          for (const key of Array.isArray(keys) ? keys : [keys]) delete store[key];
        },
      },
    },
    alarms: {
      create() {},
      onAlarm: { addListener(listener) { listeners.push(listener); } },
    },
    runtime: {
      onInstalled: { addListener(listener) { listeners.push(listener); } },
      openOptionsPage() {},
    },
    tabs: { create() {} },
    permissions: { async request() { return true; } },
  };
}

export function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() { return JSON.stringify(payload); },
  };
}

export function createFetchMock(handler) {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url, init });
    return handler(url, init, calls);
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}
