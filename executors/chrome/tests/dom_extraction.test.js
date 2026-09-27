import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { parseHTML } from 'linkedom';

function loadDomExtraction(document) {
  const sandbox = {
    globalThis: {},
    document,
    console,
  };
  vm.runInNewContext(readFileSync(new URL('../src/tools/google_maps/dom_extraction.js', import.meta.url), 'utf8'), sandbox);
  return sandbox.globalThis.__agentExecutorGoogleMapsDomExtraction;
}

test('dom extraction reads phone and address from Maps-like detail panel', () => {
  const html = `
    <div role="main">
      <h1>Hospitalis Núcleo Hospitalar Barueri</h1>
      <button data-item-id="phone:tel:+551138833322" aria-label="Telefone: (11) 3883-3322">(11) 3883-3322</button>
      <button data-item-id="address" aria-label="Endereço: Av. Zélia, 587 - Barueri, SP">Copiar endereço</button>
      <a data-item-id="authority" href="https://hospitalis.example.com/">Website</a>
    </div>
  `;
  const { document } = parseHTML(html);
  const api = loadDomExtraction(document);
  const panel = document.querySelector('[role="main"]');
  assert.match(api.extractPhone(panel), /3883/);
  assert.match(api.extractAddress(panel), /Zélia/);
  const website = api.extractWebsite(panel);
  assert.ok(!website || website.includes('hospitalis.example.com'));
});

test('dom extraction returns null when phone is absent in card', () => {
  const html = '<div role="article"><a aria-label="Hospital Municipal">Hospital Municipal</a></div>';
  const { document } = parseHTML(html);
  const api = loadDomExtraction(document);
  const card = document.querySelector('[role="article"]');
  assert.equal(api.extractPhone(card), null);
  assert.equal(api.extractAddress(card), null);
});
