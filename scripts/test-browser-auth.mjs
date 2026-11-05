import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { createRequire } from 'node:module';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

// Requires an existing playwright-core installation and an existing Edge binary.
// Uses an isolated loopback server, temporary data directory and ephemeral browser context.
const require = createRequire(import.meta.url);
const playwrightPath = process.env.TANKAI_PLAYWRIGHT_CORE;
if (!playwrightPath || !process.env.TANKAI_TEST_PYTHON || !process.env.TANKAI_TEST_EDGE) throw new Error('Set TANKAI_PLAYWRIGHT_CORE, TANKAI_TEST_PYTHON and TANKAI_TEST_EDGE');
const { chromium } = require(playwrightPath);
const tmp = await mkdtemp(path.join(os.tmpdir(), 'tankai-browser-auth-'));
const probe = createServer();
await new Promise((resolve, reject) => probe.listen(0, '127.0.0.1', resolve).on('error', reject));
const port = probe.address().port;
await new Promise(resolve => probe.close(resolve));
const base = `http://127.0.0.1:${port}`;
const env = { ...process.env, TANKAI_HOST: '127.0.0.1', TANKAI_PORT: String(port), TANKAI_DATA_ROOT: tmp, TANKAI_AUTH_DB: path.join(tmp, 'auth.db'), TANKAI_AUTH_MODE: 'session', TANKAI_COOKIE_SECURE: '0', TANKAI_ALLOW_REGISTRATION: '1', TANKAI_LLM: 'mock', TANKAI_LTM_MEMORY: '0' };
for (const key of ['TANKAI_AUTH_STATE_URL', 'TANKAI_AUTH_STATE_TOKEN', 'TANKAI_BOOTSTRAP_OWNER', 'TANKAI_CRITIC_LLM', 'TANKAI_SEARCH_PROVIDER']) delete env[key];
const server = spawn(process.env.TANKAI_TEST_PYTHON, ['-X', 'utf8', '-m', 'tankai.web.server'], { cwd: process.cwd(), env, stdio: 'ignore' });
let browser;
try {
  let ready = false;
  for (let i = 0; i < 100; i++) { try { const response = await fetch(`${base}/api/health`); if (response.ok) { assert.equal((await response.json()).registration_enabled, true); ready = true; break; } } catch {} await delay(200); }
  assert.ok(ready, 'local test server did not start');
  browser = await chromium.launch({ executablePath: process.env.TANKAI_TEST_EDGE, headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  let registers = 0;
  page.on('request', request => { if (request.url().endsWith('/api/auth/register')) registers++; });
  await page.goto(base, { waitUntil: 'domcontentloaded' });
  const toggle = page.locator('#registration-toggle');
  await toggle.waitFor({ state: 'visible' });
  await toggle.click();
  assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
  await page.locator('#email').fill('synthetic@example.invalid');
  await page.locator('#password').fill('short');
  await page.locator('#display-name').fill('Synthetic User');
  await page.locator('#tenant-name').fill('Synthetic Workspace');
  await page.locator('#loginBtn').click();
  assert.equal(await page.locator('#password').evaluate(e => e.validity.tooShort), true, 'native password constraint');
  assert.equal(registers, 0, 'invalid form must not submit');
  await page.locator('#password').fill('synthetic-password-123');
  await page.locator('#loginBtn').click();
  await page.locator('#appView').waitFor({ state: 'visible' });
  assert.equal(registers, 1);
  assert.equal(await page.locator('#profile-name').textContent(), 'Synthetic User');
  if (!(await page.locator('#settings-open').isVisible())) await page.locator('#open-sidebar').click();
  assert.equal(await page.locator('#profile-workspace').isVisible(), true);
  assert.equal(await page.locator('#profile-workspace').textContent(), 'Standard');
  await page.locator('#settings-open').click();
  await page.locator('#logoutBtn').click();
  await page.locator('#loginView').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#loginBtn').textContent(), 'Anmelden');
  await page.locator('#password').fill('synthetic-password-123');
  await page.locator('#loginBtn').click();
  await page.locator('#appView').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#profile-name').textContent(), 'Synthetic User');
  console.log('Edge browser passed: native validation, registration, visible account, logout and login.');
  await context.close();
} finally {
  if (browser) await browser.close();
  server.kill();
  await new Promise(resolve => { if (server.exitCode !== null || server.signalCode !== null) resolve(); else server.once('exit', resolve); });
  await rm(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}


