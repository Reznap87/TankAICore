import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

// Exercises the actual UI event handlers against a synthetic DOM and fake server.
// Native browser constraint validation, rendering, and HTTP are outside this test.
class Element {
  constructor() { this.hidden = false; this.disabled = false; this.required = false; this.checked = false; this.value = ''; this.textContent = ''; this.children = []; this.dataset = {}; this.handlers = {}; this.attrs = {}; this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } }; }
  addEventListener(type, fn) { this.handlers[type] = fn; }
  setAttribute(key, value) { this.attrs[key] = value; }
  replaceChildren(...children) { this.children = children; } append(...children) { this.children.push(...children); } focus() {} click() {} showModal() { this.open = true; } close() { this.open = false; } querySelectorAll() { return []; }
}
const elements = new Map();
const byId = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const document = { getElementById: byId, documentElement: { dataset: {} }, querySelectorAll: () => [], querySelector: () => ({ content: '' }), addEventListener() {}, createElement: () => new Element(), createElementNS: () => new Element() };
const calls = [];
let exportedText = '';
let registered = false;
let session = false;
let healthCalls = 0;
let loginCalls = 0;
let workspaceCalls = 0;
let directCalls = 0;
let historyCalls = 0;
const firstDirectRun = 'a'.repeat(32);
const secondDirectRun = 'b'.repeat(32);
const thirdDirectRun = 'c'.repeat(32);
let releaseInitialHealth;
let refreshInterval;
const delayedHealth = new Promise(resolve => { releaseInitialHealth = resolve; });
const user = { user: { display_name: 'Test', email: 'synthetic@example.invalid' }, workspace: { id: 'synthetic', name: 'Test' }, workspaces: [], csrf_token: 'synthetic' };
async function fetch(url, options = {}) {
  calls.push({ url, options });
  let status = 200, data = {};
  if (url === '/api/health') {
    healthCalls++;
    if (healthCalls === 1) return delayedHealth;
    data = healthCalls === 3
      ? { ok: true, registration_enabled: true, execution_mode: 'live', direct_chat_available: true, llm: '', user: 'synthetic@example.invalid', workspace: 'Test', workspace_id: 'synthetic' }
      : healthCalls === 4
        ? { ok: true, registration_enabled: true, execution_mode: 'live', direct_chat_available: true, llm: 'openai:synthetic-main', user: 'other@example.invalid', workspace: 'Other', workspace_id: 'other-workspace' }
        : healthCalls === 5
          ? { ok: true, registration_enabled: true, execution_mode: 'live', direct_chat_available: true, llm: 'openai:synthetic-main', user: 'synthetic@example.invalid', workspace: 'Test', workspace_id: 'other-workspace' }
          : healthCalls >= 6
            ? { ok: true, registration_enabled: true, execution_mode: 'live', direct_chat_available: true, llm: 'openai:synthetic-main', user: 'synthetic@example.invalid', workspace: 'Test', workspace_id: 'synthetic' }
        : { ok: true, registration_enabled: true };
  }
  else if (url === '/api/auth/me') { status = 401; data = { error: 'Anmeldung erforderlich' }; }
  else if (url === '/api/auth/register') { const payload = JSON.parse(options.body); assert.equal(payload.email, 'Synthetic@Example.Invalid'); assert.equal(payload.display_name, 'Test'); registered = true; session = true; data = user; }
  else if (url === '/api/auth/logout') { assert.equal(session, true); session = false; }
  else if (url === '/api/auth/login') { assert.equal(registered, true); assert.equal(JSON.parse(options.body).email, 'sYNTHETIC@eXAMPLE.iNVALID'); session = true; const call = loginCalls++; data = call === 0 ? { ...user, user: { ...user.user, email: 'other@example.invalid' } } : call === 1 ? { ...user, csrf_token: '' } : call === 2 ? { ...user, workspace: { ...user.workspace, id: '' } } : user; }
  else if (url === '/api/workspaces/select') { const call = workspaceCalls++; assert.equal(JSON.parse(options.body).workspace_id, 'target-workspace'); data = call === 0 ? { ...user, workspace: { id: 'other-workspace', name: 'Other' } } : { ...user, user: { ...user.user, email: 'other@example.invalid' }, workspace: { id: 'target-workspace', name: 'Other' } }; }
  else if (url === '/api/history') {
    const call = historyCalls++;
    data = call === 0
      ? [{ workspace_id: 'other-workspace', goal: 'Fremdes Gespräch', final_answer: 'Darf nicht erscheinen' }]
      : call === 1
        ? [{ workspace_id: 'synthetic', goal: 'Eigenes Gespräch', final_answer: 'Darf erscheinen' }]
        : directCalls >= 4
          ? [{ workspace_id: 'synthetic', run_id: firstDirectRun, goal: 'Direkte Frage', final_answer: 'Live-Antwort', answer_mode: 'direct', execution_mode: 'live' }]
          : [];
  }
  else if (url === '/api/chat') {
    const payload = JSON.parse(options.body);
    assert.equal(payload.message, 'Direkte Frage');
    const call = directCalls++;
    if (call === 6) assert.equal(payload.reply_to, secondDirectRun, 'a provider-attested turn remains eligible for a follow-up');
    else assert.equal('reply_to' in payload, false, 'new chats and rejected responses must not send a parent');
    data = { workspace_id: call === 0 ? 'other-workspace' : 'synthetic', goal: 'Direkte Frage', final_answer: call === 2 ? '' : 'Live-Antwort', answer_mode: 'direct', execution_mode: 'live', main_llm: call === 1 ? '' : 'openai:synthetic-main', observed_main_llms: call >= 4 ? ['openai:synthetic-main'] : undefined, model_identity_verified: call >= 4 ? true : undefined, critic_used: false, critic_llm: null, critic_independent: null, release_ready: false, duration_seconds: 1, run_id: call === 6 ? thirdDirectRun : call === 5 ? secondDirectRun : firstDirectRun, reply_to: call === 6 ? secondDirectRun : null };
  }
  else if (url === '/api/run') { assert.equal(JSON.parse(options.body).goal, 'Geprüfte Frage'); data = { workspace_id: 'synthetic', goal: 'Geprüfte Frage', final_answer: 'Gemischtes Ergebnis', execution_mode: 'mixed', release_ready: false, duration_seconds: 2 }; }
  else throw new Error('Unexpected endpoint: ' + url);
  return { ok: status < 400, status, json: async () => data };
}
const media = () => ({ matches: false, addEventListener() {} });
class Blob { constructor(parts) { this.parts = parts; } }
const context = { document, fetch, Blob, URL: { createObjectURL(blob) { exportedText = blob.parts.join(''); return 'blob:synthetic'; }, revokeObjectURL() {} }, matchMedia: media, localStorage: { getItem() { return null; }, setItem() {} }, setInterval(fn) { refreshInterval = fn; }, setTimeout() {}, clearTimeout() {}, console, requestAnimationFrame() {} };
const source = await readFile(new URL('../tankai/web/chat.js', import.meta.url), 'utf8');
vm.runInNewContext(source, context);
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
byId('email').value = 'Synthetic@Example.Invalid'; byId('password').value = 'synthetic-password'; byId('display-name').value = 'Test'; byId('tenant-name').value = 'Test';
await byId('login-form').handlers.submit({ preventDefault() {} });
assert.equal(byId('appView').hidden, false, 'registration receipt for the canonical mixed-case address must open the app');
assert.equal(byId('health').textContent, 'Modellstatus unbekannt', 'health without model identity must not claim a live connection');
assert.equal(byId('model-label').textContent, 'Kein Live-Modell verbunden');
await refreshInterval();
assert.equal(byId('health').textContent, 'Verbindung unterbrochen', 'health for another account or workspace must fail closed');
assert.equal(byId('model-label').textContent, 'Core momentan nicht erreichbar');
await refreshInterval();
assert.equal(byId('health').textContent, 'Verbindung unterbrochen', 'health for a same-named foreign workspace id must fail closed');
assert.equal(byId('model-label').textContent, 'Core momentan nicht erreichbar');
assert.equal(byId('history').children.some(child => child.textContent === 'Fremdes Gespräch'), false, 'wrong-workspace history must not be displayed');
assert.match(byId('history').textContent, /Fremder Verlauf/);
byId('search-open').handlers.click();
assert.equal(byId('search-results').children.some(child => child.textContent.includes('Fremdes Gespräch')), false, 'wrong-workspace history must not be searchable');
await byId('logoutBtn').handlers.click();
assert.equal(byId('loginView').hidden, false);
assert.equal(byId('registration-fields').hidden, true, 'Logout must return to login mode');
assert.equal(byId('loginBtn').textContent, 'Anmelden');
byId('email').value = 'sYNTHETIC@eXAMPLE.iNVALID';
byId('password').value = 'synthetic-password';
await byId('login-form').handlers.submit({ preventDefault() {} });
assert.equal(byId('appView').hidden, true, 'login receipt for another account must not open the app');
assert.match(byId('loginStatus').textContent, /Anmeldenachweis/);
await byId('login-form').handlers.submit({ preventDefault() {} });
assert.equal(byId('appView').hidden, true, 'login receipt without a CSRF token must not open the app');
assert.match(byId('loginStatus').textContent, /Anmeldenachweis/);
await byId('login-form').handlers.submit({ preventDefault() {} });
assert.equal(byId('appView').hidden, true, 'login receipt without an active workspace id must not open the app');
assert.match(byId('loginStatus').textContent, /Anmeldenachweis/);
await byId('login-form').handlers.submit({ preventDefault() {} });
assert.equal(byId('appView').hidden, false, 'login receipt for the canonical mixed-case address must open the app');
assert.equal(byId('health').textContent, 'Core verbunden');
assert.equal(byId('model-label').textContent, 'Live-Chat · openai:synthetic-main');
assert.deepEqual(calls.filter(c => c.url.startsWith('/api/auth/') && c.url !== '/api/auth/me').map(c => c.url), ['/api/auth/register', '/api/auth/logout', '/api/auth/login', '/api/auth/login', '/api/auth/login', '/api/auth/login']);
assert.equal(byId('history').children.some(child => child.textContent === 'Eigenes Gespräch'), true, 'active-workspace history must remain available');
byId('search-open').handlers.click();
assert.equal(byId('search-results').children[0].children[1].children[0].textContent, 'Eigenes Gespräch', 'active-workspace history must remain searchable');
byId('workspace').value = 'target-workspace';
await byId('workspace').handlers.change();
assert.equal(workspaceCalls, 1);
assert.equal(byId('profile-workspace').textContent, 'Test', 'a workspace receipt for another workspace must not replace the active workspace');
assert.equal(byId('workspace').value, 'synthetic', 'a rejected workspace receipt must restore the previous selection');
byId('workspace').value = 'target-workspace';
await byId('workspace').handlers.change();
assert.equal(workspaceCalls, 2);
assert.equal(byId('profile-name').textContent, 'Test', 'a workspace receipt for another account must not replace the active account');
assert.equal(byId('profile-workspace').textContent, 'Test', 'a foreign-account receipt must not replace the active workspace');
assert.equal(byId('workspace').value, 'synthetic', 'a foreign-account receipt must restore the previous workspace selection');
byId('goal').value = 'Direkte Frage'; byId('advanced').checked = false;
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('outCard').hidden, true, 'wrong-workspace answer must not be displayed');
assert.match(byId('status').textContent, /aktiven Workspace/);
byId('goal').value = 'Direkte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('outCard').hidden, true, 'live answer without model identity must not be displayed');
assert.match(byId('status').textContent, /Modell-ID/);
byId('goal').value = 'Direkte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('outCard').hidden, true, 'live response without answer text must not be displayed');
assert.match(byId('status').textContent, /Antworttext/);
byId('goal').value = 'Direkte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('outCard').hidden, true, 'live response without provider-confirmed identity proof must not be displayed');
assert.match(byId('status').textContent, /Provider-bestätigte Modellidentität/);
byId('goal').value = 'Direkte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.deepEqual(byId('badges').children.map(child => child.textContent), ['KI-Antwort · ungeprüft', 'Modellidentität providerbestätigt', 'Hauptmodell: openai:synthetic-main', 'Prüfmodell: nicht verwendet', 'Keine unabhängige Prüfung', '1 s']);
byId('export-chat').handlers.click();
assert.match(exportedText, /Prüfmodell: nicht verwendet/);
assert.match(exportedText, /Modellidentität providerbestätigt: Ja/);
assert.match(exportedText, /Prüfmodell unabhängig: Nicht geprüft/);
byId('history').children[0].handlers.click();
assert.equal(byId('badges').children[0].textContent, 'Live-Modellnachweis nicht bestätigt', 'legacy live history must not claim a verified model identity');
assert.equal(byId('badges').children[1].textContent, 'Modellidentität nicht providerbestätigt');
byId('export-chat').handlers.click();
assert.match(exportedText, /Modellidentität providerbestätigt: Nein/);
const callsBeforeLegacyFollowup = directCalls;
byId('goal').value = 'Direkte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(directCalls, callsBeforeLegacyFollowup, 'legacy live history must not be sent as follow-up context');
assert.match(byId('status').textContent, /providerbestätigten Modellnachweis.*neuen Chat/i);
assert.equal(byId('threadHistory').hidden, true, 'unattested history must not be presented as trusted thread context');
byId('new-chat').handlers.click();
assert.equal(byId('threadHistory').hidden, true, 'New Chat must clear prior messages');
byId('goal').value = 'Direkte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(directCalls, 6, 'new chat should be sent without a parent');
byId('goal').value = 'Direkte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(directCalls, 7, 'a provider-attested direct answer should remain eligible for follow-up');
byId('goal').value = 'Geprüfte Frage'; byId('advanced').checked = true;
await byId('composer').handlers.submit({ preventDefault() {} });
assert.deepEqual(byId('badges').children.map(child => child.textContent), ['Teilweise Simulation · keine verifizierte Live-Antwort', 'Hauptmodell: nicht belegt', 'Prüfmodell: nicht belegt', 'Prüfmodell-Unabhängigkeit nicht belegt', '2 s']);

const restoreElements = new Map();
const restoreById = id => { if (!restoreElements.has(id)) restoreElements.set(id, new Element()); return restoreElements.get(id); };
const restoreDocument = { ...document, getElementById: restoreById };
const restoreContext = { ...context, document: restoreDocument, setInterval() {}, fetch: async url => ({ ok: true, status: 200, json: async () => url === '/api/auth/me' ? { ...user, csrf_token: '' } : url === '/api/history' ? [] : { ok: true, registration_enabled: false } }) };
vm.runInNewContext(source, restoreContext);
await flush(); await flush();
assert.equal(restoreById('appView').hidden, true, 'session restore without a complete auth receipt must not open the app');
assert.match(restoreById('loginStatus').textContent, /Anmeldenachweis/);
console.log('Synthetic DOM auth and chat flow passed: mixed-case registration/login, foreign-receipt rejection, workspace-bound history, direct chat, fail-closed provenance.');
