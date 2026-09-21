export function getChromeApi() {
  if (!globalThis.chrome) {
    throw new Error('chrome_api_unavailable');
  }
  return globalThis.chrome;
}

export function chromeStorageArea(area = 'local', chromeApi = getChromeApi()) {
  const storage = chromeApi.storage?.[area];
  if (!storage) {
    throw new Error(`chrome_storage_${area}_unavailable`);
  }
  return storage;
}

export function storageGet(keys, chromeApi = getChromeApi()) {
  return chromeStorageArea('local', chromeApi).get(keys);
}

export function storageSet(values, chromeApi = getChromeApi()) {
  return chromeStorageArea('local', chromeApi).set(values);
}

export function storageRemove(keys, chromeApi = getChromeApi()) {
  return chromeStorageArea('local', chromeApi).remove(keys);
}
