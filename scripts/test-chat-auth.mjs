import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

// Exercises the actual UI event handlers against a synthetic DOM and fake server.
// Native browser constraint validation, rendering, and HTTP are outside this test.
class Element {
  constructor() { this.hidden = false; this.disabled = false; this.required = false; this.value = ''; this.textContent = ''; this.dataset = {}; this.handlers = {}; this.attrs = {}; this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } }; }
  addEventListener(type, fn) { this.handlers[type] = fn; }
  setAttribute(key, value) { this.attrs[key] = value; }
  replaceChildren() {} append() {} focus() {} querySelectorAll() { return []; }
}
const elements = new Map();
const byId = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const document = { getElementById: byId, documentElement: { dataset: {} }, querySelectorAll: () => [], querySelector: () => ({ content: '' }), addEventListener() {}, createElement: () => new Element(), createElementNS: () => new Element() };
const calls = [];
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
  else throw new Error('Unexpected endpoint: ' + url);
  return { ok: status < 400, status, json: async () => data };
}
const media = () => ({ matches: false, addEventListener() {} });
const context = { document, fetch, matchMedia: media, localStorage: { getItem() { return null; }, setItem() {} }, setInterval() {}, setTimeout() {}, clearTimeout() {}, console, requestAnimationFrame() {} };
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
console.log('Synthetic DOM auth flow passed: toggle, registration, logout, login.');
