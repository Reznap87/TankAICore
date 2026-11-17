import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { mkdtemp, readFile, realpath, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

// Native Edge/CDP only: no browser download or third-party automation dependency.
// All accounts, browser state and model requests stay in the loopback fixture.
if (!process.env.TANKAI_TEST_PYTHON || !process.env.TANKAI_TEST_EDGE) throw new Error('Set TANKAI_TEST_PYTHON and TANKAI_TEST_EDGE');
const tmp = await mkdtemp(path.join(os.tmpdir(), 'tankai-browser-auth-'));
const probe = createServer();
await new Promise((resolve, reject) => probe.listen(0, '127.0.0.1', resolve).on('error', reject));
const port = probe.address().port;
await new Promise(resolve => probe.close(resolve));
const base = 'http://127.0.0.1:' + port;
const env = { ...process.env, TANKAI_HOST: '127.0.0.1', TANKAI_PORT: String(port), TANKAI_DATA_ROOT: tmp, TANKAI_AUTH_DB: path.join(tmp, 'auth.db'), TANKAI_AUTH_MODE: 'session', TANKAI_COOKIE_SECURE: '0', TANKAI_ALLOW_REGISTRATION: '1', TANKAI_LLM: 'mock', TANKAI_LTM_MEMORY: '0' };
for (const key of ['TANKAI_AUTH_STATE_URL', 'TANKAI_AUTH_STATE_TOKEN', 'TANKAI_BOOTSTRAP_OWNER', 'TANKAI_CRITIC_LLM', 'TANKAI_SEARCH_PROVIDER']) delete env[key];
const server = spawn(process.env.TANKAI_TEST_PYTHON, ['-X', 'utf8', '-m', 'tankai.web.server'], { cwd: process.cwd(), env, stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true });
let serverStderr = '', serverStartError, lastHealth;
server.stderr.on('data', data => { serverStderr = (serverStderr + data.toString()).slice(-4000); });
server.once('error', error => { serverStartError = error.code || error.name; });
let browser, socket, closeBrowser;

async function until(check, label, timeout = 20000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await delay(100);
  }
  throw new Error('Timed out: ' + label);
}

async function stopProcessTree(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  if (process.platform === 'win32') {
    // The venv launcher can own another Python process; stop this exact test tree.
    const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore', windowsHide: true });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { killer.kill(); reject(new Error('Test process cleanup timed out')); }, 10000);
      killer.once('exit', () => { clearTimeout(timer); resolve(); });
      killer.once('error', error => { clearTimeout(timer); reject(error); });
    });
  } else child.kill('SIGKILL');
  await until(() => child.exitCode !== null || child.signalCode !== null, 'test process exit', 10000);
}

try {
  try { await until(async () => {
    if (serverStartError || server.exitCode !== null) throw new Error('Fixture server exited: ' + String(serverStartError || server.exitCode));
    try {
      const response = await fetch(base + '/api/health', { signal: AbortSignal.timeout(1000) });
      lastHealth = {status: response.status};
      if (!response.ok) return false;
      const health = await response.json(); lastHealth.registration_enabled = health.registration_enabled;
      assert.equal(health.registration_enabled, true);
      return true;
    } catch (error) { lastHealth = {...lastHealth, error: error.name}; return false; }
  }, 'local test server'); } catch (error) {
    console.error(JSON.stringify({fixture_exit_code: server.exitCode, fixture_start_error: serverStartError, last_health: lastHealth, fixture_stderr: serverStderr}));
    throw error;
  }
  browser = spawn(process.env.TANKAI_TEST_EDGE, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--disable-component-update', '--disable-default-apps',
    '--disable-sync', '--disable-extensions', '--metrics-recording-only',
    '--remote-debugging-port=0', '--user-data-dir=' + path.join(tmp, 'edge-profile'), 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });
  let devtools;
  await until(async () => {
    try { devtools = (await readFile(path.join(tmp, 'edge-profile', 'DevToolsActivePort'), 'utf8')).trim().split(/\r?\n/); return devtools.length === 2; }
    catch { return false; }
  }, 'isolated Edge debugging endpoint');
  socket = new WebSocket('ws://127.0.0.1:' + devtools[0] + devtools[1]);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('CDP connection timed out')), 10000);
    socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('CDP connection failed')); }, { once: true });
  });
  let nextId = 0, registers = 0, chatStatus;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject, timer } = pending.get(message.id);
      clearTimeout(timer); pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message)); else resolve(message.result);
    }
    if (message.method === 'Network.requestWillBeSent' && message.params.request.url === base + '/api/auth/register') registers++;
    if (message.method === 'Network.responseReceived' && message.params.response.url === base + '/api/chat') chatStatus = message.params.response.status;
  });
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP command timed out: ' + method)); }, 10000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  // Edge may relaunch and exit its original launcher. CDP reaches the actual
  // browser process even when the child handle no longer owns that process.
  closeBrowser = () => send('Browser.close');
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  const evaluate = async expression => {
    const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
    if (result.exceptionDetails) throw new Error('Browser evaluation failed: ' + result.exceptionDetails.text);
    return result.result.value;
  };
  const select = id => 'document.getElementById(' + JSON.stringify(id) + ')';
  const visible = id => evaluate('!!(' + select(id) + '?.getClientRects().length)');
  const waitVisible = id => until(() => visible(id), 'visible ' + id);
  const click = async id => { await waitVisible(id); await evaluate(select(id) + '.click()'); };
  const fill = async (id, value) => {
    await waitVisible(id);
    await evaluate(select(id) + '.focus(); ' + select(id) + '.select()');
    // Input.insertText is real browser input, so native minlength validation applies.
    await send('Input.insertText', { text: value }, sessionId);
  };
  const text = id => evaluate(select(id) + '.textContent');
  const assertSimulationHealth = async phase => {
    await until(async () => (await text('health')) === 'Simulation', phase + ' authenticated simulation health');
    const snapshot = await evaluate('(async () => { const h = await fetch("/api/health", {cache:"no-store"}); const m = await fetch("/api/auth/me", {cache:"no-store"}); return {healthStatus:h.status, meStatus:m.status, health:await h.json(), me:await m.json(), mode:document.getElementById("connection-info").dataset.mode}; })()');
    assert.equal(snapshot.healthStatus, 200);
    assert.equal(snapshot.meStatus, 200);
    assert.equal(snapshot.health.user, 'synthetic@example.invalid');
    assert.equal(snapshot.health.workspace, 'Standard');
    assert.equal(snapshot.health.user, snapshot.me.user.email, phase + ' health uses authenticated email');
    assert.equal(snapshot.health.workspace, snapshot.me.workspace.name, phase + ' health uses active workspace name');
    assert.equal(snapshot.health.execution_mode, 'simulation');
    assert.equal(snapshot.health.direct_chat_available, false);
    assert.equal(snapshot.mode, 'simulation', phase + ' must not report offline or live');
    assert.equal(await visible('health'), true);
    assert.equal(await text('login-health'), 'TankAI-Core erreichbar');
    assert.equal(await text('model-label'), 'Kein Live-Modell verbunden');
    console.log(JSON.stringify({phase, user:snapshot.health.user, workspace:snapshot.health.workspace, health_mode:snapshot.health.execution_mode, visible_mode:snapshot.mode}));
  };
  await send('Page.enable', {}, sessionId);
  await send('Network.enable', {}, sessionId);
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false }, sessionId);
  await send('Page.navigate', { url: base }, sessionId);
  await click('registration-toggle');
  assert.equal(await evaluate(select('registration-toggle') + '.getAttribute("aria-expanded")'), 'true');
  await fill('email', 'Synthetic@Example.Invalid');
  await fill('password', 'short');
  await fill('display-name', 'Synthetic User');
  await fill('tenant-name', 'Synthetic Workspace');
  await click('loginBtn');
  assert.equal(await evaluate(select('password') + '.validity.tooShort'), true, 'native password constraint');
  assert.equal(registers, 0, 'invalid form must not submit');
  await fill('password', 'synthetic-password-123');
  await click('loginBtn');
  await until(async () => await visible('appView') || (await text('loginStatus')).includes('Anmeldenachweis'), 'registration receipt');
  assert.equal(await visible('appView'), true, await text('loginStatus'));
  await waitVisible('appView');
  assert.equal(registers, 1);
  assert.equal(await text('profile-name'), 'Synthetic User');
  if (!(await visible('settings-open'))) await click('open-sidebar');
  assert.equal(await visible('profile-workspace'), true);
  assert.equal(await text('profile-workspace'), 'Standard');
  await assertSimulationHealth('registration');
  await click('settings-open');
  await click('logoutBtn');
  await waitVisible('loginView');
  assert.equal(await text('loginBtn'), 'Anmelden');
  await fill('email', 'SYNTHETIC@EXAMPLE.INVALID');
  await fill('password', 'synthetic-password-123');
  await click('loginBtn');
  await waitVisible('appView');
  assert.equal(await text('profile-name'), 'Synthetic User');
  await assertSimulationHealth('login');
  await fill('goal', 'Synthetic browser check: no real model call.');
  await click('runBtn');
  await until(async () => (await text('status')).includes('Kein echtes KI-Modell'), 'visible mock rejection');
  assert.equal(chatStatus, 503);
  assert.equal(await visible('outCard'), false, 'rejected mock must not show an answer');
  assert.equal(await evaluate(select('history') + '.querySelectorAll(".history-item").length'), 0, 'rejected mock must not create a successful history entry');
  assert.equal(await evaluate(select('runBtn') + '.disabled'), false, 'composer recovers after rejection');
  console.log('Edge/CDP passed: native password validation, registration, visible account, logout/login, visible mock-chat 503 rejection, no answer or successful history entry.');
} finally {
  try { if (closeBrowser) await closeBrowser(); }
  finally {
    if (socket) socket.close();
    try { await stopProcessTree(browser); }
    finally {
      await stopProcessTree(server);
      // tmp was created above by mkdtemp; no existing profile or auth database is used.
      const cleanupRoot = await realpath(tmp);
      assert.equal(path.dirname(cleanupRoot), await realpath(os.tmpdir()), 'cleanup stays in the resolved temporary parent');
      assert.equal(path.basename(cleanupRoot), path.basename(tmp), 'cleanup keeps the exact newly created directory');
      assert.match(path.basename(cleanupRoot), /^tankai-browser-auth-[A-Za-z0-9]+$/);
      await rm(cleanupRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
  }
}
