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
let advancedCalls = 0;
let historyCalls = 0;
const firstDirectRun = 'a'.repeat(32);
const secondDirectRun = 'b'.repeat(32);
const thirdDirectRun = 'c'.repeat(32);
const fourthDirectRun = 'e'.repeat(32);
const legacyRun = 'd'.repeat(32);
const advancedRun = '11111111-2222-4333-8444-555555555555';
const failedAdvancedRun = '66666666-7777-4888-8999-aaaaaaaaaaaa';
const planGateAdvancedRun = 'bbbbbbbb-cccc-4ddd-8eee-ffffffffffff';
const verifiedAdvancedRun = '12345678-1234-4abc-8def-123456789abc';
const criticMismatchAdvancedRun = 'fedcba98-7654-4cba-9876-fedcba987654';
const receiptsMismatchAdvancedRun = '0f0f0f0f-1e1e-4d2d-8c3c-4b4b4b4b4b4b';
const planMismatchAdvancedRun = '13572468-2468-4ace-8bdf-135724681357';
const durationMismatchAdvancedRun = 'abcdef01-2345-4abc-8def-0123456789ab';
const legacyHistory = { workspace_id: 'synthetic', run_id: legacyRun, goal: 'Eigenes Gespräch', final_answer: 'Darf erscheinen', answer_mode: 'direct', execution_mode: 'live' };
const storedHistory = [legacyHistory];
let latestChatResult;
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
            ? { ok: true, registration_enabled: true, execution_mode: 'live', direct_chat_available: healthCalls !== 7, llm: 'openai:synthetic-main', user: 'synthetic@example.invalid', workspace: 'Test', workspace_id: 'synthetic' }
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
      : call === 2
        ? [{ ...latestChatResult, final_answer: 'Abweichend gespeicherte Antwort' }, ...storedHistory]
        : [...storedHistory];
  }
  else if (url === '/api/chat') {
    const payload = JSON.parse(options.body);
    assert.equal(payload.message, 'Direkte Frage');
    const call = directCalls++;
    if (call === 6) assert.equal(payload.reply_to, secondDirectRun, 'a provider-attested turn remains eligible for a follow-up');
    else if (call === 7) assert.equal(payload.reply_to, thirdDirectRun, 'the next provider-attested turn remains eligible for a follow-up');
    else assert.equal('reply_to' in payload, false, 'new chats and rejected responses must not send a parent');
    data = { workspace_id: call === 0 ? 'other-workspace' : 'synthetic', goal: 'Direkte Frage', final_answer: call === 2 ? '' : 'Live-Antwort', answer_mode: 'direct', execution_mode: 'live', main_llm: call === 1 ? '' : 'openai:synthetic-main', observed_main_llms: call >= 4 ? ['openai:synthetic-main'] : undefined, model_identity_verified: call >= 4 ? true : undefined, critic_used: false, critic_llm: null, critic_independent: null, release_ready: false, duration_seconds: 1, run_id: call === 7 ? fourthDirectRun : call === 6 ? thirdDirectRun : call === 5 ? secondDirectRun : firstDirectRun, reply_to: call === 7 ? thirdDirectRun : call === 6 ? secondDirectRun : null };
    latestChatResult = data;
    if (call >= 5) storedHistory.unshift(call === 7 ? { ...data, reply_to: firstDirectRun } : data);
  }
  else if (url === '/api/run') {
    assert.equal(JSON.parse(options.body).goal, 'Geprüfte Frage');
    const call = advancedCalls++;
    const runIds = [advancedRun, failedAdvancedRun, planGateAdvancedRun, verifiedAdvancedRun, criticMismatchAdvancedRun, receiptsMismatchAdvancedRun, planMismatchAdvancedRun, durationMismatchAdvancedRun];
    data = { workspace_id: 'synthetic', run_id: runIds[call], goal: 'Geprüfte Frage', final_answer: ['Widersprüchliches Live-Ergebnis', 'Fehlgeschlagenes Live-Ergebnis', 'Plan-Gate nicht bestanden', 'Verifiziertes Live-Ergebnis', 'Abweichende Critic-Nutzung', 'Abweichender Receipt-Beleg', 'Abweichender Plan', 'Abweichende Laufzeit'][call], status: call === 1 ? 'failed' : 'completed', execution_mode: 'live', main_llm: 'openai:synthetic-main', critic_llm: 'anthropic:synthetic-critic', observed_main_llms: ['openai:synthetic-main'], observed_critic_llms: ['anthropic:synthetic-critic'], configured_main_llm: 'openai:configured-main', configured_critic_llm: 'anthropic:configured-critic', model_identity_verified: true, critic_independent: call > 0, verification_passed: call > 0, plan_gate_passed: call >= 2 ? call !== 2 : undefined, release_ready: true, duration_seconds: 2, receipts_count: call === 5 ? 1 : undefined, receipts: call === 5 ? [{ actor: 'critic', action: 'verify', success: true, output_summary: 'Bestätigt' }] : undefined, plan: call === 6 ? { rationale: 'Persistierbarer Plan', steps: [{ specialist_type: 'critic', description: 'Antwort prüfen', status: 'completed' }] } : undefined };
    storedHistory.unshift(call === 4 ? { ...data, critic_used: false } : call === 5 ? { ...data, receipts: [{ actor: 'critic', action: 'verify', success: false, output_summary: 'Fehlgeschlagen' }] } : call === 6 ? { ...data, plan: { rationale: 'Abweichend gespeichert', steps: [{ specialist_type: 'critic', description: 'Antwort prüfen', status: 'failed' }] } } : call === 7 ? { ...data, duration_seconds: 999 } : data);
  }
  else throw new Error('Unexpected endpoint: ' + url);
  return { ok: status < 400, status, json: async () => data };
}
const media = () => ({ matches: false, addEventListener() {} });
class Blob { constructor(parts) { this.parts = parts; } }
const context = { document, fetch, Blob, URL: { createObjectURL(blob) { exportedText = blob.parts.join(''); return 'blob:synthetic'; }, revokeObjectURL() {} }, matchMedia: media, localStorage: { getItem() { return null; }, setItem() {} }, setInterval(fn) { refreshInterval = fn; }, setTimeout() {}, clearTimeout() {}, console, requestAnimationFrame() {} };
const source = await readFile(new URL('../tankai/web/chat.js', import.meta.url), 'utf8');
const displayAssertions = "if (modelDisplay('openrouter_free:openrouter_free:https://openrouter.ai/api/v1') !== 'OpenRouter Free · Automatische Modellauswahl' || modelDisplay('openrouter_free:baidu/ernie:free:https://openrouter.ai/api/v1') !== 'OpenRouter Free · baidu/ernie') throw new Error('OpenRouter model label exposes URL or mislabels router');";
vm.runInNewContext(source.replace("setTheme(); applyChatSize(); syncSidebar(); refreshHealth(); loadMe();", displayAssertions + "\nsetTheme(); applyChatSize(); syncSidebar(); refreshHealth(); loadMe();"), context);
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
assert.equal(byId('model-label').textContent, 'Live-Chat bereit · konfiguriert: openai:synthetic-main');
const beforeQuestionSearch = calls.length;
byId('question-search-open').handlers.click();
assert.equal(byId('question-search-dialog').open,true);
assert.equal(byId('question-search-chat').disabled,true);
assert.equal(byId('question-search-web').hidden,true);
byId('question-search-input').value = 'KI & Technik';
byId('question-search-input').handlers.input();
assert.equal(byId('question-search-web').attrs.href,'https://duckduckgo.com/?q=KI%20%26%20Technik');
byId('goal').value='Ungesendeter Entwurf';
byId('question-search-chat').handlers.click();
assert.equal(byId('goal').value,'Ungesendeter Entwurf');
assert.match(byId('question-search-status').textContent,/ungesendeter Entwurf/);
byId('goal').value='';
byId('question-search-chat').handlers.click();
assert.equal(byId('goal').value,'KI & Technik');
assert.equal(byId('question-search-dialog').open,false);
assert.equal(calls.length,beforeQuestionSearch,'preparing a question must not send any request');
byId('new-chat').handlers.click();
const beforeLocalTools = calls.length;
byId('chat-size').value = 'large';
byId('chat-size').handlers.change();
assert.equal(document.documentElement.dataset.chatSize, 'large');
byId('chat-size').value = 'standard';
byId('chat-size').handlers.change();
assert.equal(document.documentElement.dataset.chatSize, 'standard');
byId('goal').value = 'Hallo Welt';
byId('plugins-open').handlers.click();
assert.equal(byId('plugins-dialog').open, true);
byId('plugin-analyze').handlers.click();
assert.equal(byId('plugin-result').textContent, '2 Wörter · 10 Zeichen');
byId('plugin-structure').handlers.click();
const structuredDraft = byId('goal').value;
assert.match(structuredDraft, /^Hallo Welt\n\nBitte antworte klar strukturiert/);
assert.equal(byId('plugins-dialog').open, false);
byId('composer-plugins').handlers.click();
byId('plugin-structure').handlers.click();
assert.equal(byId('goal').value, structuredDraft, 'structure plugin must be idempotent');
byId('goal').value = 'x'.repeat(20000);
byId('composer-plugins').handlers.click();
byId('plugin-structure').handlers.click();
assert.equal(byId('goal').value.length, 20000, 'plugin must respect input limit');
assert.match(byId('plugin-result').textContent, /zu lang/);
byId('plugins-dialog').close();
byId('goal').value = '';
byId('goal').handlers.input();
assert.equal(calls.length, beforeLocalTools, 'local plugins and settings must never send network requests');


assert.match(byId('sysInfo').textContent, /^Konfiguriertes Hauptmodell: openai:synthetic-main$/m);
assert.match(byId('sysInfo').textContent, /^Konfiguriertes Prüfmodell: unbekannt$/m);
assert.match(byId('sysInfo').textContent, /^Konfigurierte Modellrollen getrennt: Nein$/m);
assert.doesNotMatch(byId('sysInfo').textContent, /providerbestätigt/i, 'health must not claim per-response provider attestation');
await refreshInterval();
assert.equal(byId('model-label').textContent, 'Live-Modell konfiguriert · Chat derzeit nicht freigegeben', 'configured live model and admission state must remain distinguishable');
assert.match(byId('sysInfo').textContent, /Pilot: gemeinsame Budgetfreigabe fehlt/);
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
assert.equal(byId('outCard').hidden, true, 'an answer differing from its chat history receipt must not be displayed as completed');
assert.match(byId('status').textContent, /Verlauf/i);
byId('history').children.find(child => child.textContent === 'Eigenes Gespräch').handlers.click();
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
assert.deepEqual(byId('badges').children.map(child => child.textContent), ['KI-Antwort · ungeprüft', 'Modellidentität providerbestätigt', 'Hauptmodell: openai:synthetic-main', 'Prüfmodell: nicht verwendet', 'Keine unabhängige Prüfung', '1 s']);
byId('export-chat').handlers.click();
assert.match(exportedText, /Prüfmodell: nicht verwendet/);
assert.match(exportedText, /Modellidentität providerbestätigt: Ja/);
assert.match(exportedText, /Prüfmodell unabhängig: Nicht geprüft/);
byId('goal').value = 'Direkte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(directCalls, 7, 'a provider-attested direct answer should remain eligible for follow-up');
byId('goal').value = 'Direkte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(directCalls, 8, 'a direct answer with a mismatched persisted parent must reach the history check');
assert.equal(byId('outCard').hidden, true, 'a reply parent differing from persisted history must not be displayed as completed');
assert.match(byId('status').textContent, /Verlauf/i);
byId('goal').value = 'Geprüfte Frage'; byId('advanced').checked = true;
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('badges').children[0].textContent, 'Nicht freigegeben', 'release_ready alone must not claim a passed independent verification');
assert.equal(byId('badges').children[4].textContent, 'Prüfmodell nicht unabhängig');
assert.doesNotMatch(byId('status').textContent, /Prüfung abgeschlossen/, 'contradictory verification fields must not claim a completed review');
byId('goal').value = 'Geprüfte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('badges').children[0].textContent, 'Nicht freigegeben', 'a failed run must never claim a passed verification');
assert.doesNotMatch(byId('status').textContent, /Prüfung abgeschlossen/, 'a failed run must not claim a completed review');
byId('goal').value = 'Geprüfte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('badges').children[0].textContent, 'Nicht freigegeben', 'a failed plan gate must never claim a passed verification');
assert.doesNotMatch(byId('status').textContent, /Prüfung abgeschlossen/, 'a failed plan gate must not claim a completed review');
byId('goal').value = 'Geprüfte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('badges').children[0].textContent, 'Prüfung bestanden', 'a fully consistent advanced receipt must retain its success status');
assert.match(byId('status').textContent, /Prüfung abgeschlossen/);
byId('goal').value = 'Geprüfte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('outCard').hidden, true, 'critic usage differing from persisted history must not be displayed as completed');
assert.match(byId('status').textContent, /Verlauf/i);
byId('goal').value = 'Geprüfte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('outCard').hidden, true, 'receipt evidence differing from persisted history must not be displayed as completed');
assert.equal(byId('receiptCard').hidden, true, 'unpersisted positive receipt evidence must stay hidden');
assert.match(byId('status').textContent, /Verlauf/i);
byId('goal').value = 'Geprüfte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('outCard').hidden, true, 'plan evidence differing from persisted history must not be displayed as completed');
assert.equal(byId('planCard').hidden, true, 'unpersisted plan evidence must stay hidden');
assert.match(byId('status').textContent, /Verlauf/i);
byId('goal').value = 'Geprüfte Frage';
await byId('composer').handlers.submit({ preventDefault() {} });
assert.equal(byId('outCard').hidden, true, 'duration differing from persisted history must not be displayed as completed');
assert.match(byId('status').textContent, /Verlauf/i);

const restoreElements = new Map();
const restoreById = id => { if (!restoreElements.has(id)) restoreElements.set(id, new Element()); return restoreElements.get(id); };
const restoreDocument = { ...document, getElementById: restoreById };
const restoreContext = { ...context, document: restoreDocument, setInterval() {}, fetch: async url => ({ ok: true, status: 200, json: async () => url === '/api/auth/me' ? { ...user, csrf_token: '' } : url === '/api/history' ? [] : { ok: true, registration_enabled: false } }) };
vm.runInNewContext(source, restoreContext);
await flush(); await flush();
assert.equal(restoreById('appView').hidden, true, 'session restore without a complete auth receipt must not open the app');
assert.match(restoreById('loginStatus').textContent, /Anmeldenachweis/);
console.log('Synthetic DOM auth and chat flow passed: mixed-case registration/login, foreign-receipt rejection, workspace-bound history, direct chat, fail-closed provenance.');
