'use strict';
(() => {
const byId = id => document.getElementById(id);
const mobile = matchMedia('(max-width: 780px)');
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
let csrf = '';
let currentUser = null;
let epoch = 0;
let busy = false;
let registering = false;
let historyItems = [];
let selectedResult = null;
let selectedIndex = -1;
let theme = 'dark';
let chatSize = 'standard';
let toastTimer;
try { const stored = localStorage.getItem('tankai.ui.theme'); if (['dark', 'light', 'system'].includes(stored)) theme = stored; }
catch (error) { console.info('Theme unavailable.'); }
try { const stored = localStorage.getItem('tankai.ui.chatSize'); if (['standard', 'large'].includes(stored)) chatSize = stored; } catch (error) { console.info('Text size unavailable.'); }
function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; }
function icon(name) { const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); node.setAttribute('aria-hidden', 'true'); const use = document.createElementNS(node.namespaceURI, 'use'); use.setAttribute('href', '#i-' + name); node.append(use); return node; }
function toast(message) { clearTimeout(toastTimer); byId('toast').textContent = message; byId('toast').hidden = false; toastTimer = setTimeout(() => { byId('toast').hidden = true; }, 6000); }
function setTheme() { const value = theme === 'system' ? (systemTheme.matches ? 'dark' : 'light') : theme; document.documentElement.dataset.theme = value; byId('theme').value = theme; document.querySelector('meta[name="theme-color"]').content = value === 'dark' ? '#171b22' : '#f6f8fc'; }
function modelDisplay(identity) {
const raw = String(identity || 'nicht belegt');
const match = /^([^:]+):(.+):https?:\/\/[^\s]+$/u.exec(raw);
if (!match) return raw;
if (match[1] === 'nvidia_free') return 'NVIDIA Test · ' + match[2];
if (match[1] === 'openrouter_free') return 'OpenRouter Free · ' + (['openrouter_free', 'openrouter/free'].includes(match[2]) ? 'Automatische Modellauswahl' : match[2].replace(/:free$/, ''));
return match[1] + ': ' + match[2];
}
async function jsonFetch(url, options = {}) {
const headers = Object.assign({}, options.headers || {});
if (csrf && options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrf;
const response = await fetch(url, Object.assign({}, options, { headers, credentials: 'same-origin', cache: 'no-store' }));
let data;
try { data = await response.json(); } catch (error) { throw new Error('Der Server hat keine lesbare Antwort geliefert.'); }
if (!response.ok || data.error) { const error = new Error(data.error || ('HTTP ' + response.status)); error.status = response.status; error.retryAfter = Number(data.retry_after_seconds); throw error; }
return data;
}
function resetResult() {
selectedResult = null; selectedIndex = -1; byId('threadHistory').replaceChildren(); byId('threadHistory').hidden = true; ['questionRow', 'outCard', 'planCard', 'receiptCard'].forEach(id => { byId(id).hidden = true; }); ['question', 'answer', 'badges', 'planBody', 'receiptBody', 'rationale'].forEach(id => byId(id).replaceChildren()); byId('export-chat').disabled = true;
byId('welcome').hidden = false; byId('suggestions').hidden = false; byId('messages').hidden = true; byId('appView').classList.remove('conversation'); byId('topbar-title').textContent = 'TankAI'; document.title = 'TankAI – Dein Chat';
}
function resetWorkspace() {
const provider=byId('chat-provider'); if(provider){provider.value=''; provider.hidden=true;}
historyItems = []; byId('history').replaceChildren(); byId('search-results').replaceChildren(); byId('search-input').value = ''; byId('goal').value = ''; byId('status').textContent = ''; byId('sysInfo').textContent = ''; byId('plugin-result').textContent = ''; byId('skill-store-results').replaceChildren();
byId('health').textContent = 'Core wird geprüft'; byId('connection-info').dataset.mode = 'unknown'; byId('model-label').textContent = 'Verbindung wird geprüft'; resetResult(); renderHistory();
}
function showLogin() {
epoch += 1; csrf = ''; currentUser = null; busy = false; setRegistration(false); resetWorkspace(); byId('workspace').replaceChildren(); byId('profile-name').textContent = 'Dein Konto'; byId('profile-workspace').textContent = 'Geschützter Arbeitsraum';
document.querySelectorAll('dialog[open]').forEach(d => d.close()); byId('appView').hidden = true; byId('loginView').hidden = false; sidebar(false); setBusy(false);
}
function sessionOk(data){return data.user?.email&&data.csrf_token?.trim?.()&&data.workspace?.id}
function showApp(data) {
epoch += 1; currentUser = data; csrf = String(data.csrf_token || ''); resetWorkspace(); byId('loginView').hidden = true; byId('appView').hidden = false; byId('profile-name').textContent = data.user?.display_name || data.user?.email || 'Dein Konto'; byId('profile-workspace').textContent = data.workspace?.name || 'Dein Workspace';
const select = byId('workspace'); select.replaceChildren(); (data.workspaces || []).forEach(w => { const option = element('option', '', w.name + ' (' + w.role + ')'); option.value = w.id; option.selected = w.id === data.workspace?.id; select.append(option); }); setBusy(false); syncSidebar();
}
function setBusy(value) {
busy = value; const provider=byId('chat-provider'); if(provider)provider.disabled=value; byId('goal').disabled = value; ['new-chat', 'workspace', 'plugins-open', 'composer-plugins', 'plugin-analyze', 'plugin-structure'].forEach(id => { byId(id).disabled = value; }); document.querySelectorAll('[data-prompt]').forEach(b => { b.disabled = value; }); byId('runBtn').disabled = value || !byId('goal').value.trim(); byId('composer').setAttribute('aria-busy', String(value));
}
async function refreshHealth() {
const e=epoch;
try {
const data=await jsonFetch('/api/health'); if(e!==epoch)return;
byId('login-health').textContent=data.ok?'TankAI-Core erreichbar':'Core nicht erreichbar';
byId('registration-toggle').hidden=data.registration_enabled!==true;
if(data.registration_enabled!==true&&registering)setRegistration(false);
if(!currentUser)return;
if(data.user!==currentUser.user.email||data.workspace_id!==currentUser.workspace.id)throw 0;
const provider=byId('chat-provider'); if(provider){provider.hidden=data.nvidia_chat_available!==true; if(provider.hidden&&!busy)provider.value='';}
const m=String(data.llm||'').trim(),mode=data.execution_mode==='live'&&!m?'unknown':data.execution_mode||'unknown';
const label=mode==='live'?'Core verbunden':mode==='simulation'?'Simulation':mode==='mixed'?'Teilweise Simulation':'Modellstatus unbekannt';
byId('health').textContent=label;byId('connection-info').dataset.mode=mode;
byId('model-label').textContent=data.direct_chat_available&&m?'Live-Chat bereit · konfiguriert: '+modelDisplay(m):mode==='live'&&m?'Live-Modell konfiguriert · Chat derzeit nicht freigegeben':'Kein Live-Modell verbunden';
byId('sysInfo').textContent='Konfiguriertes Hauptmodell: '+(m||'unbekannt')+'\nKonfiguriertes Prüfmodell: '+(data.critic_llm||'unbekannt')+'\nKonfigurierte Modellrollen getrennt: '+(data.critic_independent?'Ja':'Nein')+'\nRecherche: '+(data.web_research||'disabled')+'\nModus: '+mode+'\nVersion: '+String(data.version||'');
if(data.monetary_admission_configured){try{const p=await jsonFetch('/api/pilot/preflight');if(e!==epoch)return;
const R={source_approval_unavailable:'Quelle fehlt',concurrent_job_limit:'Auftrag aktiv',pilot_not_started:'Pilot nicht begonnen',pilot_expired:'Pilot beendet',pilot_halted:'Pilot angehalten',daily_budget:'Tagesbudget aufgebraucht',total_budget:'Gesamtbudget aufgebraucht',daily_job_quota:'Tagesauftragsgrenze',total_job_quota:'Auftragsgrenze',clock_regression:'Serverzeit prüfen',main_target_not_current:'Hauptmodell prüfen',critic_target_not_current:'Prüfmodell prüfen',independent_critic_required:'Prüfmodell fehlt'};
const d=(p.blockers||[]).map(r=>R[r]||'Bedingung offen');
byId('sysInfo').textContent+='\nPilot: '+(p.admission_checks_passed?'Startbedingungen erfüllt; Berechtigung und Kosten werden vor jedem Aufruf erneut geprüft.':d.join('; ')||'Bedingungen offen');
}catch(error){if(e!==epoch)return;byId('sysInfo').textContent+='\nPilot: Prüfung derzeit nicht verfügbar.'}}
else if(mode==='live'||mode==='mixed')byId('sysInfo').textContent+='\nPilot: gemeinsame Budgetfreigabe fehlt.';
}catch(error){if(e!==epoch)return;byId('login-health').textContent='Core momentan nicht erreichbar';if(currentUser){byId('health').textContent='Verbindung unterbrochen';byId('connection-info').dataset.mode='offline';byId('model-label').textContent='Core momentan nicht erreichbar';}}
}
async function loadMe() {
const requestEpoch = epoch;
try { const data = await jsonFetch('/api/auth/me'); if (requestEpoch !== epoch) return; if(!sessionOk(data)){showLogin();byId('loginStatus').textContent='Anmeldenachweis fehlt';return} showApp(data); await Promise.all([refreshHealth(), loadHistory()]); }
catch (error) { if (requestEpoch !== epoch) return; if (error.status === 401) { showLogin(); await refreshHealth(); } else byId('loginStatus').textContent = String(error.message || error); }
}
async function login(event) {
event.preventDefault();const btn=byId('loginBtn'),toggle=byId('registration-toggle');if(btn.disabled)return;btn.disabled=true;toggle.disabled=true;const requestEpoch=epoch,isRegistration=registering,email=byId('email').value.trim(),password=byId('password').value;byId('loginStatus').textContent=isRegistration?'Konto wird erstellt …':'Anmeldung wird geprüft …';const payload=isRegistration?{email,password,display_name:byId('display-name').value.trim(),tenant_name:byId('tenant-name').value.trim()}:{email,password};
try{const data=await jsonFetch(isRegistration?'/api/auth/register':'/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});if(requestEpoch!==epoch)return;if(data.user?.email!==email.toLowerCase()||!sessionOk(data))throw new Error('Anmeldenachweis fehlt');byId('password').value='';showApp(data);await Promise.all([refreshHealth(),loadHistory()]);byId('loginStatus').textContent='';}
catch(error){if(requestEpoch===epoch)byId('loginStatus').textContent=String(error.message||error);}
finally{btn.disabled=false;toggle.disabled=false;}
}
function setRegistration(value) {
registering = value; byId('registration-fields').hidden = !value; byId('registration-toggle').setAttribute('aria-expanded', String(value)); byId('login-intro').textContent = value ? 'Erstelle dein TankAI-Konto und deinen Arbeitsraum.' : 'Melde dich mit deinem TankAI-Konto an.'; byId('display-name').required = value; byId('tenant-name').required = value;
byId('password').minLength = value ? 12 : 0; byId('password').autocomplete = value ? 'new-password' : 'current-password'; byId('loginBtn').textContent = value ? 'Konto erstellen' : 'Anmelden'; byId('registration-toggle').textContent = value ? 'Bereits ein Konto? Anmelden' : 'Konto erstellen'; byId('loginStatus').textContent = '';
}
async function logout() {
const requestEpoch = epoch;
try { await jsonFetch('/api/auth/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); if (requestEpoch === epoch) showLogin(); }
catch (error) { if (requestEpoch !== epoch) return; if (error.status === 401) showLogin(); else toast('Abmeldung nicht bestätigt: ' + String(error.message || error)); }
}
async function selectWorkspace() {
if (busy || !currentUser) return;
const requestEpoch=epoch,oldId=currentUser.workspace.id,mail=currentUser.user.email,w=byId('workspace').value;byId('workspace').disabled=true;
try{const data=await jsonFetch('/api/workspaces/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:w})});if(requestEpoch!==epoch)return;if(!sessionOk(data)||data.user.email!==mail||data.workspace.id!==w)throw new Error('Workspace-Nachweis fehlt');showApp(data);await Promise.all([refreshHealth(),loadHistory()])}
catch (error) { if (requestEpoch !== epoch) return; if (error.status === 401) showLogin(); else { byId('workspace').value = oldId; toast('Workspace konnte nicht gewechselt werden: ' + String(error.message || error)); } }
finally { if (currentUser && !busy) byId('workspace').disabled = false; }
}
function renderHistory() {
const root = byId('history'); root.replaceChildren();
if (!historyItems.length) { const empty = element('div', 'empty-history'); empty.append(icon('chat'), element('span', '', 'Deine Gespräche aus diesem Workspace erscheinen hier.')); root.append(empty); return; }
historyItems.forEach((item, index) => { const button = element('button', 'history-item' + (index === selectedIndex ? ' active' : ''), String(item.goal || 'Ohne Titel').slice(0, 65)); button.type = 'button'; button.disabled = busy; if (index === selectedIndex) button.setAttribute('aria-current', 'page'); button.addEventListener('click', () => { selectedIndex = index; renderResult(item); renderHistory(); if (mobile.matches) sidebar(false); }); root.append(button); });
}
function resultKey(item) { return item.run_id || JSON.stringify([item.goal, item.final_answer, item.duration_seconds]); }
function sameWs(item) { const id = currentUser?.workspace?.id; return id && item?.workspace_id === id; }
const historyReceiptFields = ['run_id', 'workspace_id', 'goal', 'final_answer', 'status', 'execution_mode', 'answer_mode', 'reply_to', 'critic_used', 'release_ready', 'verification_passed', 'plan_gate_passed', 'main_llm', 'critic_llm', 'observed_main_llms', 'observed_critic_llms', 'configured_main_llm', 'configured_critic_llm', 'model_identity_verified', 'critic_independent', 'duration_seconds', 'receipts_count', 'receipts', 'plan'];
function historyReceiptIndex(data, expected) {
if(!expected||typeof expected.run_id!=='string'||!expected.run_id)return -1;
const matches=data.map((item,index)=>({item,index})).filter(({item})=>item&&item.run_id===expected.run_id);
if(matches.length!==1)return -1;
return historyReceiptFields.every(key=>JSON.stringify(matches[0].item[key])===JSON.stringify(expected[key]))?matches[0].index:-1;
}
async function loadHistory(expectedResult = null) {
const e = epoch;
try { const data = await jsonFetch('/api/history'); if (e !== epoch) return false; if (!Array.isArray(data) || !data.every(sameWs)) throw new Error('Fremder Verlauf'); historyItems = data; selectedIndex = expectedResult ? historyReceiptIndex(data, expectedResult) : selectedResult ? data.findIndex(item => resultKey(item) === resultKey(selectedResult)) : -1; if (selectedResult) renderThread(selectedResult); renderHistory(); return !expectedResult || selectedIndex >= 0; }
catch (error) { if (e !== epoch) return false; if (error.status === 401) showLogin(); else { historyItems = []; selectedIndex = -1; byId('history').textContent = 'Verlauf nicht verfügbar: ' + String(error.message || error); } return false; }
}
function textCell(value) { return element('td', '', value ?? ''); }
function badge(value, className) { return element('span', 'badge ' + className, value); }
function renderThread(d) {
const root=byId('threadHistory'), index=new Map(historyItems.filter(sameWs).map(x=>[x.run_id,x]));
const turns=[], seen=new Set(); let id=d.answer_mode==='direct'?d.reply_to:null;
while(id && turns.length<4 && !seen.has(id)){
seen.add(id); const x=index.get(id);
if(!x || x.answer_mode!=='direct' || x.execution_mode!=='live' || !providerIdentityVerified(x,false) || typeof x.goal!=='string' || typeof x.final_answer!=='string')break;
turns.unshift(x); id=x.reply_to;
}
root.replaceChildren();
for(const x of turns){const q=element('div','message'),a=element('article','answer-card');q.append(element('div','message-bubble',x.goal));a.append(element('div','answer-content',x.final_answer));root.append(q,a)}
root.hidden=!turns.length;
}
function providerIdentityVerified(data, criticUsed) {
const main=String(data.main_llm||'').trim(), observedMain=data.observed_main_llms;
if(data.model_identity_verified!==true||!main||!Array.isArray(observedMain)||observedMain.length!==1||observedMain[0]!==main)return false;
if(!criticUsed)return true;
const critic=String(data.critic_llm||'').trim(), observedCritic=data.observed_critic_llms;
return !!critic&&Array.isArray(observedCritic)&&observedCritic.length===1&&observedCritic[0]===critic&&critic!==main;
}
function releaseVerified(data) {
return data.answer_mode!=='direct'&&data.status==='completed'&&data.execution_mode==='live'&&data.critic_used!==false&&providerIdentityVerified(data,true)&&data.critic_independent===true&&data.plan_gate_passed===true&&data.verification_passed===true&&data.release_ready===true;
}
function renderResult(data) {
selectedResult = data; renderThread(data); byId('welcome').hidden = true; byId('suggestions').hidden = true; byId('messages').hidden = false; byId('appView').classList.add('conversation');
byId('questionRow').hidden = false; byId('question').textContent = String(data.goal || '');
byId('outCard').hidden = false; byId('answer').textContent = String(data.final_answer || 'Der Core hat keinen Antworttext geliefert.');
const badges = byId('badges'); badges.replaceChildren(); const mode = String(data.execution_mode || 'unknown').toLowerCase(); const simulated = data.status === 'simulated' || mode === 'simulation' || mode === 'mixed'; const direct = data.answer_mode === 'direct'; const criticUsed = !direct && data.critic_used !== false; const identityVerified=mode==='live'&&providerIdentityVerified(data,criticUsed); const verifiedRelease=releaseVerified(data);
const resultLabel = mode === 'mixed' ? 'Teilweise Simulation · keine verifizierte Live-Antwort' : simulated ? 'Simulation · keine verifizierte KI-Antwort' : mode !== 'live' ? 'Ausführungsmodus nicht belegt' : !identityVerified ? 'Live-Modellnachweis nicht bestätigt' : direct ? 'KI-Antwort · ungeprüft' : verifiedRelease ? 'Prüfung bestanden' : 'Nicht freigegeben';
badges.append(badge(resultLabel, verifiedRelease ? 'b-ok' : 'b-warn'));
if(mode==='live')badges.append(badge(identityVerified?'Modellidentität providerbestätigt':'Modellidentität nicht providerbestätigt',identityVerified?'b-ok':'b-warn'));
badges.append(badge('Hauptmodell: ' + modelDisplay(data.main_llm), data.main_llm ? 'b-info' : 'b-warn'));
badges.append(badge(criticUsed ? 'Prüfmodell: ' + modelDisplay(data.critic_llm) : 'Prüfmodell: nicht verwendet', criticUsed && data.critic_llm ? 'b-info' : 'b-warn'));
badges.append(badge(!criticUsed ? 'Keine unabhängige Prüfung' : data.critic_independent === true ? 'Prüfmodell unabhängig' : data.critic_independent === false ? 'Prüfmodell nicht unabhängig' : 'Prüfmodell-Unabhängigkeit nicht belegt', criticUsed && data.critic_independent === true ? 'b-ok' : 'b-warn'));
badges.append(badge(String(data.duration_seconds ?? 0) + ' s', 'b-info'));
const steps = data.plan?.steps; const planBody = byId('planBody'); planBody.replaceChildren(); byId('planCard').hidden = !Array.isArray(steps) || !steps.length;
byId('rationale').textContent = String(data.plan?.rationale || '');
if (Array.isArray(steps)) steps.forEach((step, index) => { const row = element('tr'); row.append(textCell(index + 1), textCell(step.specialist_type), textCell(step.description), textCell(step.status || '—')); planBody.append(row); });
const receiptBody = byId('receiptBody'); receiptBody.replaceChildren(); const receipts = Array.isArray(data.receipts) ? data.receipts : []; byId('receiptCard').hidden = !receipts.length;
receipts.forEach(receipt => { const row = element('tr'); row.append(textCell(receipt.actor), textCell(receipt.action), textCell(receipt.success ? '✓' : '✗'), textCell(String(receipt.output_summary || '').slice(0, 300))); receiptBody.append(row); });
const title = String(data.goal || 'Gespräch').replace(/\s+/g, ' ').slice(0, 55); byId('topbar-title').textContent = title; document.title = title + ' – TankAI'; byId('export-chat').disabled = false;
requestAnimationFrame(() => { byId('messages').scrollTop = byId('messages').scrollHeight; });
}
async function runTank(event) {
event.preventDefault(); const goal = byId('goal').value.trim(); if (busy || !goal || !currentUser) return;
const requestEpoch = epoch; const definition = byId('dod').value.trim(); const parallel = byId('parallel').checked; const advanced = byId('advanced').checked; const selectedDirect = !advanced && selectedResult?.answer_mode === 'direct'; const parentRun = selectedDirect && selectedResult.execution_mode === 'live' && providerIdentityVerified(selectedResult,false) && typeof selectedResult.run_id === 'string' && /^[0-9a-f]{32}$/.test(selectedResult.run_id) ? selectedResult.run_id : null;
const provider=byId('chat-provider'); const nvidia=!advanced&&provider&&!provider.hidden&&['nvidia_free','nvidia_glm'].includes(provider.value);
if(parentRun && (String(selectedResult.main_llm||'').startsWith('nvidia_free:')!==Boolean(nvidia))){byId('status').textContent='Bei Anbieterwechsel bitte einen neuen Chat starten.';return;}
if(selectedDirect&&!parentRun){byId('status').textContent='Dieser ältere Verlauf hat keinen providerbestätigten Modellnachweis und kann nicht als Kontext verwendet werden. Bitte starte einen neuen Chat.';return}
resetResult(); setBusy(true); renderHistory();
byId('welcome').hidden = true; byId('suggestions').hidden = true; byId('messages').hidden = false; byId('appView').classList.add('conversation'); byId('questionRow').hidden = false; byId('question').textContent = goal;
byId('status').textContent = advanced ? 'TankAI bearbeitet deine Aufgabe und prüft das Ergebnis …' : nvidia ? 'TankAI fragt '+(provider.value==='nvidia_glm'?'GLM 5.3 Flash':'GPT-OSS-20B')+' (NVIDIA-Testbetrieb) …' : 'TankAI fragt das Live-Modell …';
try {
const data = await jsonFetch(advanced ? '/api/run' : '/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(advanced ? { goal, definition_of_done: definition, parallel } : { message: goal, ...(parentRun ? {reply_to:parentRun} : {}), ...(nvidia ? {provider:provider.value} : {}) }) });
if (requestEpoch !== epoch) return;
if (!sameWs(data)) throw new Error('Antwort stammt nicht aus dem aktiven Workspace');
if((!advanced||data.execution_mode==='live')&&['main_llm','final_answer'].some(k=>typeof data[k]!=='string'||!data[k].trim()))throw new Error('Modell-ID oder Antworttext fehlt');
if(data.execution_mode==='live'&&!providerIdentityVerified(data,advanced))throw new Error('Provider-bestätigte Modellidentität fehlt oder ist innerhalb des Runs nicht stabil');
if (!data.goal) data.goal = goal;
if(!await loadHistory(data))throw new Error('Antwort konnte nicht identisch aus dem Chat-Verlauf bestätigt werden');
renderResult(data); byId('goal').value = '';
byId('status').textContent = data.status === 'simulated' || data.execution_mode === 'simulation' ? 'Simulation beendet. Es wurde kein verifiziertes Live-Ergebnis erzeugt.' : data.answer_mode === 'direct' ? 'Live-Antwort erhalten · nicht unabhängig geprüft.' : releaseVerified(data) ? 'Antwort und Prüfung abgeschlossen.' : 'Ergebnis erhalten. Bitte den angezeigten Prüfstatus beachten.';
} catch (error) { if (requestEpoch !== epoch) return; if (error.status === 401) showLogin(); else if(error.status===429&&Number.isFinite(error.retryAfter)&&error.retryAfter>0) byId('status').textContent='Anfragelimit erreicht. Erneut versuchen in etwa '+Math.ceil(error.retryAfter/60)+' Min. Es erfolgt kein automatischer Anbieterwechsel.'; else byId('status').textContent = 'Aufgabe nicht bestätigt: ' + String(error.message || error) + '. Bei einer Verbindungsstörung vor erneutem Senden zuerst den Verlauf prüfen.'; }
finally { if (requestEpoch === epoch) { setBusy(false); renderHistory(); byId('goal').focus(); } }
}
function newChat() { if (busy) return; resetResult(); byId('goal').value = ''; byId('status').textContent = ''; setBusy(false); renderHistory(); if (mobile.matches) sidebar(false); byId('goal').focus(); }
function sidebar(open) { if (mobile.matches) { byId('appView').classList.toggle('sidebar-open', open); byId('backdrop').hidden = !open; byId('sidebar').inert = !open; byId('main').inert = open; } else { byId('appView').classList.toggle('sidebar-collapsed', !open); byId('sidebar').inert = !open; } byId('open-sidebar').setAttribute('aria-expanded', String(open)); if (open && mobile.matches) byId('close-sidebar').focus(); if (!open && !byId('appView').hidden) byId('open-sidebar').focus({ preventScroll: true }); }
function syncSidebar() { byId('appView').classList.remove('sidebar-open'); byId('main').inert = false; byId('backdrop').hidden = true; const open = !mobile.matches && !byId('appView').classList.contains('sidebar-collapsed'); byId('sidebar').inert = !open; byId('open-sidebar').setAttribute('aria-expanded', String(open)); }
function showDialog(id) { if (currentUser) byId(id).showModal(); }
function applyChatSize() { document.documentElement.dataset.chatSize = chatSize; byId('chat-size').value = chatSize; }
async function loadSkills(action, skillId) {
const e = epoch, root = byId('skill-store-results');
if (!currentUser || busy) return;
root.textContent = 'Skills werden geladen …';
try {
const data = await jsonFetch('/api/skills', action ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({skill_id:skillId,action})} : {});
if (e !== epoch) return;
if (data.workspace_id !== currentUser.workspace.id || !Array.isArray(data.skills)) throw new Error('Skill-Nachweis fehlt');
root.replaceChildren();
for (const skill of data.skills) {
const card = element('div','settings-section');
card.append(element('strong','',skill.id),element('p','',skill.title),element('p','muted',skill.enabled?'Aktiv für Entwurfshilfe':skill.imported?'Importiert · inaktiv':'Im Katalog verfügbar'));
const source = element('a','','NVIDIA-Original');
const url = new URL(skill.source);
if (url.origin === 'https://github.com' && url.pathname.startsWith('/NVIDIA/skills/blob/')) { source.href = url.href; source.target = '_blank'; source.rel = 'noopener noreferrer'; card.append(source); }
if (skill.imported && skill.document_available) {
const read = element('button','secondary-button','Original-Skill ansehen');
read.type='button';
const preview = element('pre','skill-document'); preview.hidden=true;
read.addEventListener('click',async()=>{
if(e!==epoch || !currentUser || !root.contains(card))return;
if(!preview.hidden){preview.hidden=true;return;}
read.disabled=true; preview.textContent='Dokument wird geladen …'; preview.hidden=false;
try{
const doc=await jsonFetch('/api/skills/document?id='+encodeURIComponent(skill.id));
if(e!==epoch || !root.contains(card))return;
if(doc.workspace_id!==currentUser.workspace.id || doc.id!==skill.id || doc.revision!==skill.revision || typeof doc.content!=='string' || doc.execution_available!==false)throw new Error('Dokumentnachweis fehlt');
preview.textContent=doc.content;
}catch(error){if(e===epoch && root.contains(card))preview.textContent=String(error.message||error);}
finally{read.disabled=false;}
}); card.append(read,preview);
}
const action = skill.enabled?'disable':skill.imported?'enable':'import';
const toggle = element('button','secondary-button',action==='disable'?'Deaktivieren':action==='enable'?'Aktivieren':'Skill importieren');
toggle.type='button'; toggle.addEventListener('click',()=>loadSkills(action,skill.id)); card.append(toggle);
if (skill.enabled && typeof skill.guidance === 'string') {
const use = element('button','secondary-button','Entwurfshilfe einfügen'); use.type='button';
use.addEventListener('click',()=>{
if (e!==epoch || busy || !currentUser) return;
const goal=byId('goal'); const next=goal.value+(goal.value?'\n\n':'')+'Skill-Entwurfshilfe ('+skill.id+'): '+skill.guidance;
if (next.length>20000) { toast('Entwurf zu lang'); return; }
goal.value=next; setBusy(false); byId('plugins-dialog').close(); goal.focus();
}); card.append(use);
}
if(skill.enabled && skill.local_adapter_available){
const examples={
'rag-eval':{ranked_ids:['doc1','doc2'],relevant_ids:['doc2']},
'nemo-retriever':{query:'Beispiel',documents:[{id:'doc1',text:'Ein Beispieltext'}]},
'data-designer':{required_fields:['id','text'],rows:[{id:1,text:'Beispiel'}]},
'rag-blueprint':{source_inventory:false,access_control:false,index_strategy:false,retrieval_evaluation:false,deletion_policy:false}
};
const input=element('textarea'), output=element('pre','skill-document'), run=element('button','secondary-button','Lokalen Adapter ausführen');
input.setAttribute('aria-label','Skill-Eingabe als JSON'); input.rows=6; input.maxLength=60000;
input.value=JSON.stringify(examples[skill.id],null,2); run.type='button';
run.addEventListener('click',async()=>{
if(e!==epoch||!currentUser||busy||!root.contains(card))return;
run.disabled=true; output.textContent='Berechnung läuft …';
try{
const payload=JSON.parse(input.value);
const result=await jsonFetch('/api/skills/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({skill_id:skill.id,payload})});
if(e!==epoch||!root.contains(card))return;
if(result.workspace_id!==currentUser.workspace.id||result.skill_id!==skill.id||result.adapter!==skill.local_adapter||result.external_calls!==0||result.nvidia_execution!==false)throw new Error('Runner-Nachweis fehlt');
output.textContent=JSON.stringify(result,null,2);
}catch(error){if(e===epoch&&root.contains(card))output.textContent=String(error.message||error);}
finally{run.disabled=false;}
});
card.append(element('p','muted','Lokaler TankAI-Adapter; keine NVIDIA-Laufzeit, keine externen Aufrufe. Eingabe als JSON.'),input,run,output);
}
root.append(card);
}
const enabled=new Set(data.skills.filter(skill=>skill.enabled).map(skill=>skill.id));
const workflow=element('div','settings-section');
workflow.append(element('h3','','Lokaler Skill-Workflow'),element('p','muted','Dokumente suchen und die Treffer automatisch bewerten. Keine Modellaufrufe oder externe Übertragung durch den Workflow.'));
if(!enabled.has('nemo-retriever')||!enabled.has('rag-eval')){
workflow.append(element('p','','Aktiviere nemo-retriever und rag-eval.'));
}else{
const input=element('textarea'),output=element('pre','skill-document'),run=element('button','secondary-button','Suche und Bewertung starten'),use=element('button','secondary-button','Ergebnis in Chat-Entwurf übernehmen');
input.rows=7; input.maxLength=60000; input.setAttribute('aria-label','Workflow-Eingabe als JSON');
input.value=JSON.stringify({query:'Beispiel',documents:[{id:'doc1',text:'Beispieltext'},{id:'doc2',text:'Ein Beispiel'}],relevant_ids:['doc2']},null,2);
run.type='button'; use.type='button'; use.disabled=true; let summary=null;
run.addEventListener('click',async()=>{
if(e!==epoch||!currentUser||busy||!root.contains(workflow))return;
run.disabled=true; use.disabled=true; summary=null; output.textContent='Workflow läuft …';
try{
const result=await jsonFetch('/api/skills/workflow',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(JSON.parse(input.value))});
if(e!==epoch||!root.contains(workflow))return;
if(result.workspace_id!==currentUser.workspace.id||result.workflow_id!=='retrieval-evaluation'||result.execution_mode!=='local_deterministic'||result.external_calls!==0||result.nvidia_execution!==false||!Array.isArray(result.steps)||result.steps.length!==2||result.steps[0].skill_id!=='nemo-retriever'||result.steps[1].skill_id!=='rag-eval'||!result.steps.every(step=>step.status==='completed'))throw new Error('Workflow-Nachweis fehlt');
const metrics=result.steps[1].result;
if(!['precision','recall','reciprocal_rank'].every(k=>typeof metrics?.[k]==='number'&&Number.isFinite(metrics[k])&&metrics[k]>=0&&metrics[k]<=1))throw new Error('Metrik-Nachweis fehlt');
summary=JSON.stringify({precision:metrics.precision,recall:metrics.recall,reciprocal_rank:metrics.reciprocal_rank,top_k:result.top_k});
output.textContent=JSON.stringify(result,null,2); use.disabled=false;
}catch(error){if(e===epoch&&root.contains(workflow))output.textContent=String(error.message||error);}
finally{run.disabled=false;}
});
use.addEventListener('click',()=>{
if(e!==epoch||!currentUser||busy||!root.contains(workflow)||!summary)return;
const goal=byId('goal'), next=goal.value+(goal.value?'\n\n':'')+'Erkläre diese lokal berechneten Retrieval-Metriken (keine NVIDIA-Ausführung): '+summary;
if(next.length>20000){toast('Entwurf zu lang');return;}
goal.value=next; setBusy(false); byId('plugins-dialog').close(); goal.focus();
});
workflow.append(input,run,output,use,element('p','muted','Nur numerische Metriken werden in den Entwurf übernommen. Erst Senden startet den regulären Chat.'));
}
root.append(workflow);
} catch(error) { if(e===epoch)root.textContent='Skill-Store nicht verfügbar: '+String(error.message||error); }
}
function openPlugins() { if (!currentUser) return; byId('plugin-result').textContent = ''; showDialog('plugins-dialog'); }
function analyzeDraft() {
if (!currentUser || busy) return;
const value = byId('goal').value; const trimmed = value.trim();
const words = trimmed ? trimmed.split(/\s+/u).length : 0;
byId('plugin-result').textContent = words + ' Wörter · ' + Array.from(value).length + ' Zeichen';
}
function structureDraft() {
if (!currentUser || busy) return;
const goal = byId('goal'); const value = goal.value;
if (!value.trim()) { byId('plugin-result').textContent = 'Schreibe zuerst eine Nachricht.'; return; }
const suffix = '\n\nBitte antworte klar strukturiert: Kernaussage, Begründung und konkrete nächste Schritte. Kennzeichne Unsicherheiten.';
if (!value.endsWith(suffix)) {
if (value.length + suffix.length > 20000) { byId('plugin-result').textContent = 'Der Entwurf ist zu lang. Kürze ihn zuerst.'; return; }
goal.value = value + suffix;
}
setBusy(false); byId('plugins-dialog').close(); goal.focus();
}
function renderSearch() {
const query = byId('search-input').value.trim().toLocaleLowerCase('de'); const root = byId('search-results'); root.replaceChildren();
const matches = historyItems.map((item, index) => ({ item, index })).filter(({ item }) => (String(item.goal || '') + ' ' + String(item.final_answer || '')).toLocaleLowerCase('de').includes(query));
if (!matches.length) { root.append(element('div', 'no-results', historyItems.length ? 'Keine passenden Gespräche gefunden.' : 'Noch keine Gespräche in diesem Workspace.')); return; }
matches.forEach(({ item, index }) => { const button = element('button', 'search-result'); button.type = 'button'; button.disabled = busy; button.append(icon('chat')); const text = element('div'); text.append(element('strong', '', String(item.goal || 'Ohne Titel').slice(0, 90)), element('p', '', String(item.final_answer || '').slice(0, 120))); button.append(text); button.addEventListener('click', () => { byId('search-dialog').close(); selectedIndex = index; renderResult(item); renderHistory(); if (mobile.matches) sidebar(false); }); root.append(button); });
}
function exportChat() { if (!selectedResult) return; const data = selectedResult; const criticUsed = data.answer_mode !== 'direct' && data.critic_used !== false; const critic = criticUsed ? String(data.critic_llm || 'nicht belegt') : 'nicht verwendet'; const independence = criticUsed ? (data.critic_independent === true ? 'Ja' : data.critic_independent === false ? 'Nein' : 'Nicht belegt') : 'Nicht geprüft'; const identityVerified=String(data.execution_mode||'').toLowerCase()==='live'&&providerIdentityVerified(data,criticUsed); const text = 'TankAI\n\nAufgabe\n' + String(data.goal || '') + '\n\nAntwort\n' + String(data.final_answer || '') + '\n\nModus: ' + String(data.execution_mode || 'unbekannt') + '\nHauptmodell: ' + String(data.main_llm || 'nicht belegt') + '\nPrüfmodell: ' + critic + '\nModellidentität providerbestätigt: ' + (identityVerified ? 'Ja' : 'Nein') + '\nPrüfmodell unabhängig: ' + independence + '\nStatus: ' + String(data.status || 'unbekannt') + '\nFreigegeben: ' + (data.release_ready ? 'Ja' : 'Nein') + '\n'; const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' })); const a = element('a'); a.href = url; a.download = 'TankAI-Gespraech.txt'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1500); }
byId('login-form').addEventListener('submit', login); byId('registration-toggle').addEventListener('click', () => setRegistration(!registering)); byId('logoutBtn').addEventListener('click', logout); byId('workspace').addEventListener('change', selectWorkspace);
byId('composer').addEventListener('submit', runTank); byId('goal').addEventListener('input', () => { byId('runBtn').disabled = busy || !byId('goal').value.trim(); });
byId('goal').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); byId('composer').requestSubmit(); } });
byId('new-chat').addEventListener('click', newChat); byId('export-chat').addEventListener('click', exportChat);
byId('open-sidebar').addEventListener('click', () => sidebar(true)); byId('close-sidebar').addEventListener('click', () => sidebar(false)); byId('backdrop').addEventListener('click', () => sidebar(false));
byId('search-open').addEventListener('click', () => { renderSearch(); showDialog('search-dialog'); byId('search-input').focus(); }); byId('search-input').addEventListener('input', renderSearch);
byId('settings-open').addEventListener('click', () => showDialog('settings-dialog')); byId('connection-info').addEventListener('click', () => showDialog('settings-dialog')); byId('task-options').addEventListener('click', () => showDialog('settings-dialog'));
byId('plugins-open').addEventListener('click', openPlugins); byId('composer-plugins').addEventListener('click', openPlugins);
byId('skills-load').addEventListener('click', () => loadSkills());
byId('plugin-analyze').addEventListener('click', analyzeDraft); byId('plugin-structure').addEventListener('click', structureDraft);
byId('chat-size').addEventListener('change', () => { const value = byId('chat-size').value; if (!['standard', 'large'].includes(value)) return; chatSize = value; applyChatSize(); try { localStorage.setItem('tankai.ui.chatSize', chatSize); } catch (error) { toast('Textgröße kann nicht dauerhaft gespeichert werden.'); } });
byId('theme').addEventListener('change', () => { theme = byId('theme').value; setTheme(); try { localStorage.setItem('tankai.ui.theme', theme); } catch (error) { toast('Erscheinungsbild kann in diesem Browser nicht dauerhaft gespeichert werden.'); } }); systemTheme.addEventListener('change', setTheme); mobile.addEventListener('change', syncSidebar);
document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => byId(button.dataset.close).close()));
document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => { if (!busy) { byId('goal').value = button.dataset.prompt; setBusy(false); byId('goal').focus(); } }));
document.addEventListener('keydown', event => {
if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && currentUser && !document.querySelector('dialog[open]')) { event.preventDefault(); renderSearch(); showDialog('search-dialog'); byId('search-input').focus(); }
if (event.key === 'Escape' && mobile.matches && byId('appView').classList.contains('sidebar-open') && !document.querySelector('dialog[open]')) sidebar(false);
if (event.key === 'Tab' && mobile.matches && byId('appView').classList.contains('sidebar-open') && !document.querySelector('dialog[open]')) { const buttons = [...byId('sidebar').querySelectorAll('button:not(:disabled),a[href]')]; const first = buttons[0]; const last = buttons[buttons.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } }
});
setTheme(); applyChatSize(); syncSidebar(); refreshHealth(); loadMe(); setInterval(refreshHealth, 15000);
})();
