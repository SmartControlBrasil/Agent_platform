import { storageGet, storageRemove, storageSet } from './chrome_api.js';

const CREDENTIAL_KEY = 'agentExecutorCredential';

export class CredentialStore {
  constructor(chromeApi) {
    this.chromeApi = chromeApi;
  }

  async getCredential() {
    const data = await storageGet([CREDENTIAL_KEY], this.chromeApi);
    return data[CREDENTIAL_KEY] || '';
  }

  async setCredential(credential) {
    const value = String(credential || '').trim();
    if (!value) throw new Error('credential_required');
    await storageSet({ [CREDENTIAL_KEY]: value }, this.chromeApi);
  }

  async clearCredential() {
    await storageRemove([CREDENTIAL_KEY], this.chromeApi);
  }
}
