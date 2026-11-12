import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

// Exercises the actual UI event handlers against a synthetic DOM and fake server.
// Native browser constraint validation, rendering, and HTTP are outside this test.
class Element {
  constructor() { this.hidden = false; this.disabled = false; this.required = false; this.checked = false; this.value = ''; this.textContent = ''; this.children = []; this.dataset = {}; this.handlers = {}; this.attrs = {}; this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } }; }
  addEventListener(type, fn) { this.handlers[type] = fn; }
  setAttribute(key, value) { this.attrs[key] = value; }
  replaceChildren(...children) { this.children = children; } append(...children) { this.children.push(...children); } focus() {} click() {} querySelectorAll() { return []; }
}
const elements = new Map();
const byId = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const document = { getElementById: byId, documentElement: { dataset: {} }, querySelectorAll: () => [], querySelector: () => ({ content: '' }), addEventListener() {}, createElement: () => new Element(), createElementNS: () => new Element() };
const calls = [];
let exportedText = '';
let registered = false;
let session = false;
let healthCalls = 0;
let releaseInitialHealth;
const delayedHealth = new Promise(resolve => { releaseInitialHealth = resolve; });
const user = { user: { display_name: 'Test' }, workspace: { id: 'synthetic', name: 'Test' }, workspaces: [], csrf_token: 'synthetic' };
async function fetch(url, options = {}) {
  calls.push({ url, options });
  let status = 200, data = {};
  if (url === '/api/health') {
    healthCalls++;
    if (healthCalls === 1) return delayedHealth;
    data = { ok: true, registration_enabled: true };
  }
  else if (url === '/api/auth/me') { status = 401; data = { error: 'Anmeldung erforderlich' }; }
  else if (url === '/api/auth/register') { assert.equal(JSON.parse(options.body).display_name, 'Test'); registered = true; session = true; data = user; }
  else if (url === '/api/auth/logout') { assert.equal(session, true); session = false; }
  else if (url === '/api/auth/login') { assert.equal(registered, true); session = true; data = user; }
  else if (url === '/api/history') data = [];
  else if (url === '/api/chat') { assert.equal(JSON.parse(options.body).message, 'Direkte Frage'); data = { goal: 'Direkte Frage', final_answer: 'Live-Antwort', answer_mode: 'direct', execution_mode: 'live', main_llm: 'openai:synthetic-main', critic_used: false, critic_llm: null, critic_independent: null, release_ready: false, duration_seconds: 1 }; }
  else if (url === '/api/run') { assert.equal(JSON.parse(options.body).goal, 'Geprüfte Frage'); data = { goal: 'Geprüfte Frage', final_answer: 'Gemischtes Ergebnis', execution_mode: 'mixed', release_ready: false, duration_seconds: 2 }; }
  else throw new Error('Unexpected endpoint: ' + url);
  return { ok: status < 400, status, json: async () => data };
}
const media = () => ({ matches: false, addEventListener() {} });
class Blob { constructor(parts) { this.parts = parts; } }
const context = { document, fetch, Blob, URL: { createObjectURL(blob) { exportedText = blob.parts.join(''); return 'blob:synthetic'; }, revokeObjectURL() {} }, matchMedia: media, localStorage: { getItem() { return null; }, setItem() {} }, setInterval() {}, setTimeout() {}, clearTimeout() {}, console, requestAnimationFrame() {} };
vm.runInNewContext(await readFile(new URL('../tankai/web/chat.js', import.meta.url), 'utf8'), context);
const flush = () => new Promise(resolve => setImmediate(resolve));
await flush(); await flush();
assert.equal(healthCalls, 2, '401 must trigger a fresh health check');
assert.equal(byId('registration-toggle').hidden, false);
releaseInitialHealth({ ok: true, status: 200, json: async () => ({ ok: true, registration_enabled: false }) });
await flush(); await flush();
assert.equal(byId('registration-toggle').hidden, false, 'stale health result must not hide registration');
byId('registration-toggle').handlers.click();
assert.equal(byId('registration-fields').hidden, false);
assert.equal(byId('registration-toggle').attrs['aria-expanded'], 'true');
assert.equal(byId('password').minLength, 12);
assert.equal(byId('display-name').required, true);
byId('email').value = 'synthetic@example.invalid'; byId('password').value = 'synthetic-password'; byId('display-name').value = 'Test'; byId('tenant-name').value = 'Test';
await byId('login-form').handlers.submit({ preventDefault() {} });
assert.equal(byId('appView').hidden, false);
await byId('logoutBtn').handlers.click();
assert.equal(byId('loginView').hidden, false);
assert.equal(byId('registration-fields').hidden, true, 'Logout must return to login mode');
assert.equal(byId('loginBtn').textContent, 'Anmelden');
byId('password').value = 'synthetic-password';
await byId('login-form').handlers.submit({ preventDefault() {} });
assert.equal(byId('appView').hidden, false);
assert.deepEqual(calls.filter(c => c.url.startsWith('/api/auth/') && c.url !== '/api/auth/me').map(c => c.url), ['/api/auth/register', '/api/auth/logout', '/api/auth/login']);
byId('goal').value = 'Direkte Frage'; byId('advanced').checked = false;
await byId('composer').handlers.submit({ preventDefault() {} });
assert.deepEqual(byId('badges').children.map(child => child.textContent), ['KI-Antwort · ungeprüft', 'Hauptmodell: openai:synthetic-main', 'Prüfmodell: nicht verwendet', 'Keine unabhängige Prüfung', '1 s']);
byId('export-chat').handlers.click();
assert.match(exportedText, /Prüfmodell: nicht verwendet/);
assert.match(exportedText, /Prüfmodell unabhängig: Nicht geprüft/);
byId('goal').value = 'Geprüfte Frage'; byId('advanced').checked = true;
await byId('composer').handlers.submit({ preventDefault() {} });
assert.deepEqual(byId('badges').children.map(child => child.textContent), ['Teilweise Simulation · keine verifizierte Live-Antwort', 'Hauptmodell: nicht belegt', 'Prüfmodell: nicht belegt', 'Prüfmodell-Unabhängigkeit nicht belegt', '2 s']);
console.log('Synthetic DOM auth and chat flow passed: registration, login, direct chat, fail-closed provenance.');
