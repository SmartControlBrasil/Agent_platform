import test from 'node:test';
import assert from 'node:assert/strict';
import { parseHTML } from 'linkedom';
import { setState } from '../src/state.js';
import { createChromeMock } from './helpers.js';

const POPUP_HTML = `
  <span id="state"></span>
  <span id="server"></span>
  <span id="executor"></span>
  <span id="heartbeat"></span>
  <span id="capabilities"></span>
  <span id="last-execution"></span>
  <span id="error"></span>
  <span id="status-dot"></span>
  <div id="pairing-box"></div>
  <span id="pairing-code"></span>
  <button id="configure"></button>
  <button id="pair"></button>
  <button id="disconnect"></button>
  <button id="open-platform"></button>
`;

test('open platform button opens Hando panel from configured base URL', async () => {
  const { document } = parseHTML(POPUP_HTML);
  const tabs = [];
  const chrome = createChromeMock();
  chrome.tabs.create = (payload) => tabs.push(payload);
  chrome.runtime.openOptionsPage = () => {};
  globalThis.document = document;
  globalThis.chrome = chrome;
  try {
    await setState({ platformBaseUrl: 'https://agents.smartcontrolbrasil.com.br/' }, chrome);
    await import(`../src/popup.js?test=${Date.now()}`);
    await Promise.resolve();

    document.querySelector('#open-platform').click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.deepEqual(tabs, [{ url: 'https://agents.smartcontrolbrasil.com.br/painel/' }]);
  } finally {
    delete globalThis.document;
    delete globalThis.chrome;
  }
});
