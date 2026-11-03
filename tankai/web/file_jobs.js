'use strict';
(() => {
  const el = id => document.getElementById(id);
  const optionNames = ['trim', 'removeBlank', 'removeDuplicates', 'protectFormulas'];
  let csrf = '', active = null, busy = false, dirty = false, requestId = null, scope = '';
  const node = (tag, text) => { const item = document.createElement(tag); item.textContent = String(text); return item; };
  const tell = text => { el('status').textContent = text; };
  async function api(path, body) {
    const headers = scope ? {'X-TankAI-Workspace': scope.split('/')[1]} : {};
    const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', headers,
      ...(body === undefined ? {} : {method: 'POST', headers: {...headers, 'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(body)})});
    const data = await response.json();
    if (!response.ok) { if (response.status === 401) location.replace('/'); throw new Error(data.error || 'Die Anfrage ist fehlgeschlagen.'); }
    return data;
  }
  async function verifyScope() {
    const me = await api('/api/auth/me');
    if (scope && scope !== me.user.id + '/' + me.workspace.id) {
      active = null; el('detail').hidden = true; el('jobs').replaceChildren();
      throw new Error('Der Arbeitsraum wurde in einem anderen Tab gewechselt. Bitte diese Seite neu laden.');
    }
    scope = me.user.id + '/' + me.workspace.id; csrf = me.csrf_token;
    el('workspace').textContent = me.workspace.name;
    return me;
  }
  function controls() {
    document.querySelectorAll('button,input,select,textarea').forEach(item => { item.disabled = busy; });
    optionNames.forEach(key => { el(key).disabled = busy || !active || active.completed; });
    el('preview').disabled = busy || !active || active.completed;
    el('complete').disabled = busy || !active || active.completed || dirty || !active.valid;
    el('pending').hidden = !dirty;
  }
  async function task(action) {
    if (busy) return;
    busy = true; controls();
    try { await verifyScope(); await action(); }
    catch (error) { tell(error.message || String(error)); }
    finally { busy = false; controls(); }
  }
  async function list() {
    const data = await api('/api/file-jobs'); el('jobs').replaceChildren();
    if (!data.jobs.length) el('jobs').append(node('p', 'Noch keine Dateiaufträge.'));
    data.jobs.forEach(job => {
      const button = node('button', job.name); button.type = 'button'; button.className = 'job';
      button.setAttribute('aria-current', String(active?.id === job.id));
      button.append(node('small', job.completed ? 'Ergebnis bereit' : 'Gespeicherte Auswahl'));
      button.addEventListener('click', () => task(async () => {
        if (dirty && !confirm('Nicht gespeicherte Auswahl verwerfen und diesen Auftrag öffnen?')) return;
        await open(job.id); tell('Gespeicherter Auftrag geladen.');
      })); el('jobs').append(button);
    });
  }
  async function open(id) {
    active = await api('/api/file-jobs/' + id); dirty = false; render(); await list();
  }
  function render() {
    el('detail').hidden = false; el('name').textContent = active.name;
    el('saved-note').textContent = active.goal;
    el('state').textContent = active.completed ? 'Abgeschlossen · Datei und Bericht sind unveränderlich gespeichert.' : 'Auswahl gespeichert · Version ' + active.revision;
    optionNames.forEach(key => { el(key).checked = active.options[key]; });
    el('findings').replaceChildren();
    [['Datenzeilen', active.report.inputRows], ['Leere Zeilen', active.findings.blankRows], ['Exakte Duplikate vor Bereinigung', active.findings.duplicateRows], ['Abweichende Spaltenzahl', active.findings.unevenRows]].forEach(([title, value]) => el('findings').append(node('span', title + ': ' + value)));
    el('columns').replaceChildren(); const header = document.createElement('tr');
    active.preview.header.forEach(value => header.append(node('th', value))); el('columns').append(header);
    el('rows').replaceChildren(); active.preview.rows.forEach(row => {
      const tr = document.createElement('tr'); row.forEach(value => tr.append(node('td', value))); el('rows').append(tr);
    });
    el('errors').replaceChildren(); active.errors.forEach(message => el('errors').append(node('p', message)));
    const r = active.report;
    el('changes').textContent = 'Ergebnis: ' + r.outputRows + ' Zeilen · ' + r.changedCells + ' Zellen von Leerzeichen bereinigt · ' + r.blankRemoved + ' leere Zeilen entfernt · ' + r.duplicatesRemoved + ' Duplikate entfernt · ' + r.formulaCellsChangedOnCsvExport + ' Zellen beim Export geschützt.';
    el('downloads').hidden = !active.completed; el('complete').hidden = active.completed;
    el('preview').hidden = active.completed;
    el('csv-download').href = '/api/file-jobs/' + active.id + '/csv?workspace=' + encodeURIComponent(scope.split('/')[1]);
    el('report-download').href = '/api/file-jobs/' + active.id + '/report?workspace=' + encodeURIComponent(scope.split('/')[1]);
    el('checksum').textContent = active.deliveryReport ? 'SHA-256 der Ergebnisdatei: ' + active.deliveryReport.csvSha256 : '';
  }
  el('upload').addEventListener('submit', event => { event.preventDefault(); task(async () => {
    if (dirty && !confirm('Nicht gespeicherte Auswahl verwerfen und einen neuen Auftrag anlegen?')) return;
    const file = el('source').files[0];
    if (!file || !file.name.toLowerCase().endsWith('.csv') || file.size < 1 || file.size > 2000000) throw new Error('Bitte eine CSV-Datei mit höchstens 2 MB wählen.');
    tell('Datei wird geprüft und in deinem Arbeitsraum gespeichert …');
    let source;
    try { source = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(await file.arrayBuffer()); }
    catch (error) { throw new Error('Bitte eine lesbare CSV in UTF-8 verwenden.'); }
    const job = await api('/api/file-jobs', {requestId: requestId || (requestId = crypto.randomUUID()), name: file.name, goal: el('note').value, source, delimiter: el('delimiter').value});
    requestId = null; el('source').value = ''; await open(job.id);
    tell(active.valid ? 'Datei geprüft. Wähle die erlaubten Änderungen und prüfe die Vorschau.' : 'Datei gespeichert. Die angezeigten Fehler müssen vor dem Export behoben werden.');
  }); });
  ['source', 'note', 'delimiter'].forEach(id => el(id).addEventListener('change', () => { requestId = null; }));
  optionNames.forEach(key => el(key).addEventListener('change', () => { dirty = true; controls(); }));
  el('preview').addEventListener('click', () => task(async () => {
    const options = Object.fromEntries(optionNames.map(key => [key, el(key).checked]));
    await api('/api/file-jobs/' + active.id + '/preview', {revision: active.revision, options});
    await open(active.id); tell(active.valid ? 'Auswahl gespeichert. Die Vorschau entspricht dem geplanten Ergebnis.' : 'Auswahl gespeichert. Bitte die angezeigten Fehler prüfen.');
  }));
  el('complete').addEventListener('click', () => task(async () => {
    await api('/api/file-jobs/' + active.id + '/complete', {revision: active.revision});
    await open(active.id); tell('Ergebnisdatei und zugehöriger Prüfbericht sind bereit.');
  }));
  el('remove').addEventListener('click', () => task(async () => {
    if (!confirm('Diesen Auftrag mit Ursprungsdatei und Ergebnis vom privaten Server löschen?')) return;
    await api('/api/file-jobs/' + active.id + '/delete', {}); active = null; dirty = false; el('detail').hidden = true; await list(); tell('Auftrag gelöscht.');
  }));
  // Downloads need a fresh scope check too: another tab may have selected another workspace.
  ['csv-download', 'report-download'].forEach(id => el(id).addEventListener('click', event => {
    event.preventDefault(); const href = el(id).href;
    task(async () => { const a = document.createElement('a'); a.href = href; a.download = ''; document.body.append(a); a.click(); a.remove(); });
  }));
  task(async () => { const me = await verifyScope(); if (!me.file_jobs_enabled) throw new Error('Dateiaufträge sind hier nicht eingeschaltet.'); await list(); tell('Bereit. Wähle eine CSV oder öffne einen gespeicherten Auftrag.'); });
})();
