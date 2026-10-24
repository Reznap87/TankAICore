'use strict';
(() => {
  const byId = id => document.getElementById(id);
  const mobile = matchMedia('(max-width: 780px)');
  const systemTheme = matchMedia('(prefers-color-scheme: dark)');
  let csrf = '';
  let currentUser = null;
  let epoch = 0;
  let busy = false;
  let historyItems = [];
  let selectedResult = null;
  let selectedIndex = -1;
  let theme = 'dark';
  let toastTimer;
  try { const stored = localStorage.getItem('tankai.ui.theme'); if (['dark', 'light', 'system'].includes(stored)) theme = stored; }
  catch (error) { console.info('TankAI: appearance preference is unavailable.'); }
  function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; }
  function icon(name) { const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); node.setAttribute('aria-hidden', 'true'); const use = document.createElementNS(node.namespaceURI, 'use'); use.setAttribute('href', '#i-' + name); node.append(use); return node; }
  function toast(message) { clearTimeout(toastTimer); byId('toast').textContent = message; byId('toast').hidden = false; toastTimer = setTimeout(() => { byId('toast').hidden = true; }, 6000); }
  function setTheme() { const value = theme === 'system' ? (systemTheme.matches ? 'dark' : 'light') : theme; document.documentElement.dataset.theme = value; byId('theme').value = theme; document.querySelector('meta[name="theme-color"]').content = value === 'dark' ? '#1b1d1b' : '#f8f9f6'; }
  async function jsonFetch(url, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    if (csrf && options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrf;
    const response = await fetch(url, Object.assign({}, options, { headers, credentials: 'same-origin', cache: 'no-store' }));
    let data;
    try { data = await response.json(); } catch (error) { throw new Error('Der Server hat keine lesbare Antwort geliefert.'); }
    if (!response.ok || data.error) { const error = new Error(data.error || ('HTTP ' + response.status)); error.status = response.status; throw error; }
    return data;
  }
  function resetResult() {
    selectedResult = null; selectedIndex = -1;
    ['questionRow', 'outCard', 'planCard', 'receiptCard'].forEach(id => { byId(id).hidden = true; });
    ['question', 'answer', 'badges', 'planBody', 'receiptBody', 'rationale'].forEach(id => byId(id).replaceChildren());
    byId('export-chat').disabled = true;
    byId('welcome').hidden = false; byId('suggestions').hidden = false; byId('messages').hidden = true;
    byId('appView').classList.remove('conversation'); byId('topbar-title').textContent = 'TankAI';
    document.title = 'TankAI – Dein Chat';
  }
  function resetWorkspace() {
    byId('file-jobs-link').hidden = true;
    historyItems = []; byId('history').replaceChildren(); byId('search-results').replaceChildren();
    byId('search-input').value = ''; byId('goal').value = ''; byId('status').textContent = ''; byId('sysInfo').textContent = '';
    byId('health').textContent = 'Core wird geprüft'; byId('connection-info').dataset.mode = 'unknown'; byId('model-label').textContent = 'Verbindung wird geprüft';
    resetResult(); renderHistory();
  }
  function showLogin() {
    epoch += 1; csrf = ''; currentUser = null; busy = false;
    resetWorkspace(); byId('workspace').replaceChildren(); byId('profile-name').textContent = 'Dein Konto'; byId('profile-workspace').textContent = 'Geschützter Arbeitsraum';
    document.querySelectorAll('dialog[open]').forEach(d => d.close());
    byId('appView').hidden = true; byId('loginView').hidden = false; sidebar(false); setBusy(false);
  }
  function showApp(data) {
    epoch += 1; currentUser = data; csrf = String(data.csrf_token || ''); resetWorkspace();
    byId('loginView').hidden = true; byId('appView').hidden = false;
    byId('profile-name').textContent = data.user?.display_name || data.user?.email || 'Dein Konto';
    byId('profile-workspace').textContent = data.workspace?.name || 'Dein Workspace';
    byId('file-jobs-link').hidden = !data.file_jobs_enabled;
    const select = byId('workspace'); select.replaceChildren();
    (data.workspaces || []).forEach(w => { const option = element('option', '', w.name + ' (' + w.role + ')'); option.value = w.id; option.selected = w.id === data.workspace?.id; select.append(option); });
    setBusy(false); syncSidebar();
  }
  function setBusy(value) {
    busy = value; byId('goal').disabled = value;
    ['new-chat', 'workspace'].forEach(id => { byId(id).disabled = value; });
    document.querySelectorAll('[data-prompt]').forEach(b => { b.disabled = value; });
    byId('runBtn').disabled = value || !byId('goal').value.trim();
    byId('composer').setAttribute('aria-busy', String(value));
  }
  async function refreshHealth() {
    const requestEpoch = epoch;
    try {
      const data = await jsonFetch('/api/health'); if (requestEpoch !== epoch) return;
      byId('login-health').textContent = data.ok ? 'TankAI-Core erreichbar' : 'Core nicht erreichbar';
      if (!currentUser) return;
      const mode = data.execution_mode || 'unknown';
      const label = mode === 'live' ? 'Core verbunden' : mode === 'simulation' ? 'Simulation' : mode === 'mixed' ? 'Teilweise Simulation' : 'Modellstatus unbekannt';
      byId('health').textContent = label; byId('connection-info').dataset.mode = mode;
      byId('model-label').textContent = mode === 'live' ? String(data.llm || 'Live-Modell') : mode === 'simulation' ? 'Simulation · kein Live-Modell' : label;
      byId('sysInfo').textContent = 'Hauptmodell: ' + (data.llm || 'unbekannt') + '\nPrüfmodell: ' + (data.critic_llm || 'unbekannt') + '\nUnabhängige Prüfung: ' + (data.critic_independent ? 'Ja' : 'Nein') + '\nRecherche: ' + (data.web_research || 'disabled') + '\nModus: ' + mode + '\nVersion: ' + String(data.version || '');
    } catch (error) { if (requestEpoch !== epoch) return; byId('login-health').textContent = 'Core momentan nicht erreichbar'; if (currentUser) { byId('health').textContent = 'Verbindung unterbrochen'; byId('connection-info').dataset.mode = 'offline'; byId('model-label').textContent = 'Core momentan nicht erreichbar'; } }
  }
  async function loadMe() {
    const requestEpoch = epoch;
    try { const data = await jsonFetch('/api/auth/me'); if (requestEpoch !== epoch) return; showApp(data); await Promise.all([refreshHealth(), loadHistory()]); }
    catch (error) { if (requestEpoch !== epoch) return; if (error.status === 401) showLogin(); else byId('loginStatus').textContent = String(error.message || error); }
  }
  async function login(event) {
    event.preventDefault(); const btn = byId('loginBtn'); btn.disabled = true; const requestEpoch = ++epoch;
    const email = byId('email').value.trim(); const password = byId('password').value;
    byId('loginStatus').textContent = 'Anmeldung wird geprüft …';
    try { const data = await jsonFetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password }) }); if (requestEpoch !== epoch) return; byId('password').value = ''; showApp(data); await Promise.all([refreshHealth(), loadHistory()]); byId('loginStatus').textContent = ''; }
    catch (error) { if (requestEpoch === epoch) byId('loginStatus').textContent = String(error.message || error); }
    finally { btn.disabled = false; }
  }
  async function logout() {
    const requestEpoch = epoch;
    try { await jsonFetch('/api/auth/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); if (requestEpoch === epoch) showLogin(); }
    catch (error) { if (requestEpoch !== epoch) return; if (error.status === 401) showLogin(); else toast('Abmeldung nicht bestätigt: ' + String(error.message || error)); }
  }
  async function selectWorkspace() {
    if (busy || !currentUser) return;
    const requestEpoch = epoch; const oldId = currentUser.workspace.id; byId('workspace').disabled = true;
    try { const data = await jsonFetch('/api/workspaces/select', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workspace_id: byId('workspace').value }) }); if (requestEpoch !== epoch) return; showApp(data); await Promise.all([refreshHealth(), loadHistory()]); }
    catch (error) { if (requestEpoch !== epoch) return; if (error.status === 401) showLogin(); else { byId('workspace').value = oldId; toast('Workspace konnte nicht gewechselt werden: ' + String(error.message || error)); } }
    finally { if (currentUser && !busy) byId('workspace').disabled = false; }
  }
  function renderHistory() {
    const root = byId('history'); root.replaceChildren();
    if (!historyItems.length) { const empty = element('div', 'empty-history'); empty.append(icon('chat'), element('span', '', 'Deine Gespräche aus diesem Workspace erscheinen hier.')); root.append(empty); return; }
    historyItems.forEach((item, index) => { const button = element('button', 'history-item' + (index === selectedIndex ? ' active' : ''), String(item.goal || 'Ohne Titel').slice(0, 65)); button.type = 'button'; button.disabled = busy; if (index === selectedIndex) button.setAttribute('aria-current', 'page'); button.addEventListener('click', () => { selectedIndex = index; renderResult(item); renderHistory(); if (mobile.matches) sidebar(false); }); root.append(button); });
  }
  function resultKey(item) { return item.run_id || JSON.stringify([item.goal, item.final_answer, item.duration_seconds]); }
  async function loadHistory() {
    const requestEpoch = epoch;
    try { const data = await jsonFetch('/api/history'); if (requestEpoch !== epoch) return; historyItems = Array.isArray(data) ? data : []; selectedIndex = selectedResult ? historyItems.findIndex(item => resultKey(item) === resultKey(selectedResult)) : -1; renderHistory(); }
    catch (error) { if (requestEpoch !== epoch) return; if (error.status === 401) showLogin(); else byId('history').textContent = 'Verlauf nicht verfügbar: ' + String(error.message || error); }
  }
  function textCell(value) { return element('td', '', value ?? ''); }
  function badge(value, className) { return element('span', 'badge ' + className, value); }
  function renderResult(data) {
    selectedResult = data; byId('welcome').hidden = true; byId('suggestions').hidden = true; byId('messages').hidden = false; byId('appView').classList.add('conversation');
    byId('questionRow').hidden = false; byId('question').textContent = String(data.goal || '');
    byId('outCard').hidden = false; byId('answer').textContent = String(data.final_answer || 'Der Core hat keinen Antworttext geliefert.');
    const badges = byId('badges'); badges.replaceChildren(); const simulated = data.status === 'simulated' || data.execution_mode === 'simulation';
    badges.append(badge(simulated ? 'Simulation · keine verifizierte KI-Antwort' : data.release_ready ? 'Prüfung bestanden' : 'Nicht freigegeben', simulated ? 'b-warn' : data.release_ready ? 'b-ok' : 'b-warn'));
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
    const requestEpoch = epoch; const definition = byId('dod').value.trim(); const parallel = byId('parallel').checked;
    resetResult(); setBusy(true); renderHistory();
    byId('welcome').hidden = true; byId('suggestions').hidden = true; byId('messages').hidden = false; byId('appView').classList.add('conversation'); byId('questionRow').hidden = false; byId('question').textContent = goal;
    byId('status').textContent = 'TankAI bearbeitet deine Aufgabe und prüft das Ergebnis …';
    try {
      const data = await jsonFetch('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ goal, definition_of_done: definition, parallel }) });
      if (requestEpoch !== epoch) return;
      if (!data.goal) data.goal = goal; renderResult(data); byId('goal').value = '';
      byId('status').textContent = data.status === 'simulated' || data.execution_mode === 'simulation' ? 'Simulation beendet. Es wurde kein verifiziertes Live-Ergebnis erzeugt.' : data.release_ready ? 'Antwort und Prüfung abgeschlossen.' : 'Ergebnis erhalten. Bitte den angezeigten Prüfstatus beachten.';
      await loadHistory();
    } catch (error) { if (requestEpoch !== epoch) return; if (error.status === 401) showLogin(); else byId('status').textContent = 'Aufgabe nicht bestätigt: ' + String(error.message || error) + '. Bei einer Verbindungsstörung vor erneutem Senden zuerst den Verlauf prüfen.'; }
    finally { if (requestEpoch === epoch) { setBusy(false); renderHistory(); byId('goal').focus(); } }
  }
  function newChat() { if (busy) return; resetResult(); byId('goal').value = ''; byId('status').textContent = ''; setBusy(false); renderHistory(); if (mobile.matches) sidebar(false); byId('goal').focus(); }
  function sidebar(open) { if (mobile.matches) { byId('appView').classList.toggle('sidebar-open', open); byId('backdrop').hidden = !open; byId('sidebar').inert = !open; byId('main').inert = open; } else { byId('appView').classList.toggle('sidebar-collapsed', !open); byId('sidebar').inert = !open; } byId('open-sidebar').setAttribute('aria-expanded', String(open)); if (open && mobile.matches) byId('close-sidebar').focus(); if (!open && !byId('appView').hidden) byId('open-sidebar').focus({ preventScroll: true }); }
  function syncSidebar() { byId('appView').classList.remove('sidebar-open'); byId('main').inert = false; byId('backdrop').hidden = true; const open = !mobile.matches && !byId('appView').classList.contains('sidebar-collapsed'); byId('sidebar').inert = !open; byId('open-sidebar').setAttribute('aria-expanded', String(open)); }
  function showDialog(id) { if (currentUser) byId(id).showModal(); }
  function renderSearch() {
    const query = byId('search-input').value.trim().toLocaleLowerCase('de'); const root = byId('search-results'); root.replaceChildren();
    const matches = historyItems.map((item, index) => ({ item, index })).filter(({ item }) => (String(item.goal || '') + ' ' + String(item.final_answer || '')).toLocaleLowerCase('de').includes(query));
    if (!matches.length) { root.append(element('div', 'no-results', historyItems.length ? 'Keine passenden Gespräche gefunden.' : 'Noch keine Gespräche in diesem Workspace.')); return; }
    matches.forEach(({ item, index }) => { const button = element('button', 'search-result'); button.type = 'button'; button.disabled = busy; button.append(icon('chat')); const text = element('div'); text.append(element('strong', '', String(item.goal || 'Ohne Titel').slice(0, 90)), element('p', '', String(item.final_answer || '').slice(0, 120))); button.append(text); button.addEventListener('click', () => { byId('search-dialog').close(); selectedIndex = index; renderResult(item); renderHistory(); if (mobile.matches) sidebar(false); }); root.append(button); });
  }
  function exportChat() { if (!selectedResult) return; const data = selectedResult; const text = 'TankAI\n\nAufgabe\n' + String(data.goal || '') + '\n\nAntwort\n' + String(data.final_answer || '') + '\n\nModus: ' + String(data.execution_mode || 'unbekannt') + '\nStatus: ' + String(data.status || 'unbekannt') + '\nFreigegeben: ' + (data.release_ready ? 'Ja' : 'Nein') + '\n'; const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' })); const a = element('a'); a.href = url; a.download = 'TankAI-Gespraech.txt'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1500); }
  byId('login-form').addEventListener('submit', login); byId('logoutBtn').addEventListener('click', logout); byId('workspace').addEventListener('change', selectWorkspace);
  byId('composer').addEventListener('submit', runTank); byId('goal').addEventListener('input', () => { byId('runBtn').disabled = busy || !byId('goal').value.trim(); });
  byId('goal').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); byId('composer').requestSubmit(); } });
  byId('new-chat').addEventListener('click', newChat); byId('export-chat').addEventListener('click', exportChat);
  byId('open-sidebar').addEventListener('click', () => sidebar(true)); byId('close-sidebar').addEventListener('click', () => sidebar(false)); byId('backdrop').addEventListener('click', () => sidebar(false));
  byId('search-open').addEventListener('click', () => { renderSearch(); showDialog('search-dialog'); byId('search-input').focus(); }); byId('search-input').addEventListener('input', renderSearch);
  byId('settings-open').addEventListener('click', () => showDialog('settings-dialog')); byId('connection-info').addEventListener('click', () => showDialog('settings-dialog')); byId('task-options').addEventListener('click', () => showDialog('settings-dialog'));
  byId('theme').addEventListener('change', () => { theme = byId('theme').value; setTheme(); try { localStorage.setItem('tankai.ui.theme', theme); } catch (error) { toast('Erscheinungsbild kann in diesem Browser nicht dauerhaft gespeichert werden.'); } }); systemTheme.addEventListener('change', setTheme); mobile.addEventListener('change', syncSidebar);
  document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => byId(button.dataset.close).close()));
  document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => { if (!busy) { byId('goal').value = button.dataset.prompt; setBusy(false); byId('goal').focus(); } }));
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && currentUser && !document.querySelector('dialog[open]')) { event.preventDefault(); renderSearch(); showDialog('search-dialog'); byId('search-input').focus(); }
    if (event.key === 'Escape' && mobile.matches && byId('appView').classList.contains('sidebar-open') && !document.querySelector('dialog[open]')) sidebar(false);
    if (event.key === 'Tab' && mobile.matches && byId('appView').classList.contains('sidebar-open') && !document.querySelector('dialog[open]')) { const buttons = [...byId('sidebar').querySelectorAll('button:not(:disabled),a[href]')]; const first = buttons[0]; const last = buttons[buttons.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } }
  });
  setTheme(); syncSidebar(); refreshHealth(); loadMe(); setInterval(refreshHealth, 15000);
})();
