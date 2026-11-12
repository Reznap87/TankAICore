// Integration test against the actual bundled Worker and Durable Object runtime.
// Run after npm run cf:check. No production resources or credentials are used.
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

const directory = await mkdtemp(join(tmpdir(), 'tankai-auth-test-'));
const token = 'a'.repeat(43);
const options = {
  name: 'tankai-auth-test',
  modules: true, scriptPath: resolve('dist/cloudflare.js'),
  compatibilityDate: '2026-08-20', compatibilityFlags: ['nodejs_compat'],
  bindings: { TANKAI_AUTH_STATE_TOKEN: token, TANKAI_ALLOW_REGISTRATION: '1' },
  durableObjects: { TANKAI_AUTH_STATE: { className: 'TankAIAuthState', useSQLite: true, unsafeUniqueKey: 'tankai-auth-state-test' } },
  resourcePersistencePath: directory,
};
let runtime = new Miniflare(convertV4MiniflareOptions(options));
const call = (method, body, revision = '0', authorization = `Bearer ${token}`) => runtime.dispatchFetch('https://tankaicore.com/_tankai/auth-state', {
  method, headers: { Authorization: authorization, 'If-Match': revision, ...(body ? { 'Content-Length': String(body.byteLength) } : {}) }, ...(body ? { body } : {}),
});
try {
  assert.equal((await call('GET', null, '0', 'Bearer wrong')).status, 403);
  assert.equal((await call('GET')).status, 404);
  const bytes = new Uint8Array(64 * 1024 + 300);
  bytes.set(new TextEncoder().encode('SQLite format 3\0'));
  bytes[65540] = 123;
  const saved = await call('PUT', bytes);
  assert.equal(saved.status, 204);
  assert.equal(saved.headers.get('X-Auth-Revision'), '1');
  assert.equal((await call('PUT', bytes)).status, 409);
  await runtime.dispose();
  runtime = new Miniflare(convertV4MiniflareOptions(options));
  const restored = await call('GET');
  assert.equal(restored.status, 200);
  assert.deepEqual(new Uint8Array(await restored.arrayBuffer()), bytes);
  assert.equal((await call('PUT', new Uint8Array(20), '1')).status, 400);
  assert.equal((await call('POST')).status, 405);
  const registration = (ip) => runtime.dispatchFetch('https://tankaicore.com/api/auth/register', {
    method: 'POST', headers: ip ? { 'CF-Connecting-IP': ip } : {},
  });
  assert.equal((await registration()).status, 503);
  // The test has no container binding, so permitted requests reach the container
  // path and return 503. The limiter must still reject the fourth attempt.
  for (let i = 0; i < 3; i++) assert.equal((await registration('192.0.2.10')).status, 503);
  assert.equal((await registration('192.0.2.10')).status, 429);
  await runtime.dispose();
  runtime = new Miniflare(convertV4MiniflareOptions(options));
  assert.equal((await registration('192.0.2.10')).status, 429);
  assert.equal((await registration('192.0.2.11')).status, 503);
  const namespace = await runtime.getDurableObjectNamespace('TANKAI_AUTH_STATE');
  const stub = namespace.get(namespace.idFromName('singleton'));
  const limit = (path, bucket, id, outcome = '') => stub.fetch(`https://tankai.internal/${path}`, {
    method: 'POST', headers: { Authorization: `Bearer ${token}`, 'X-Registration-Bucket': bucket,
      'X-Registration-Id': id, 'X-Registration-Outcome': outcome },
  });
  // Same original 12-bit bucket, distinct full keys: clients do not collide.
  const first = 'abc00000000000000000000000000000';
  const second = 'abc10000000000000000000000000000';
  for (let i = 0; i < 3; i++) {
    const id = randomUUID();
    assert.equal((await limit('registration-limit', first, id)).status, 204);
    assert.equal((await limit('registration-resolve', first, id, 'rejected')).status, 204);
    // Resolution is idempotent.
    assert.equal((await limit('registration-resolve', first, id, 'rejected')).status, 204);
  }
  assert.equal((await limit('registration-limit', first, randomUUID())).status, 429);
  assert.equal((await limit('registration-limit', second, randomUUID())).status, 204);
  // Rejected attempts from many users do not exhaust the global daily limit.
  for (let n = 0; n < 60; n++) {
    const bucket = n.toString(16).padStart(32, '0');
    const id = randomUUID();
    assert.equal((await limit('registration-limit', bucket, id)).status, 204);
    assert.equal((await limit('registration-resolve', bucket, id, 'rejected')).status, 204);
  }
  // Concurrent attempts from one client are serialized; no more than three pass.
  const race = 'fed00000000000000000000000000000';
  const concurrent = await Promise.all(Array.from({ length: 10 }, () => limit('registration-limit', race, randomUUID())));
  assert.equal(concurrent.filter(response => response.status === 204).length, 3);
  assert.equal(concurrent.filter(response => response.status === 429).length, 7);
  await runtime.dispose();
  runtime = new Miniflare(convertV4MiniflareOptions(options));
  const restartedNamespace = await runtime.getDurableObjectNamespace('TANKAI_AUTH_STATE');
  const restarted = restartedNamespace.get(restartedNamespace.idFromName('singleton'));
  const afterRestart = await restarted.fetch('https://tankai.internal/registration-limit', {
    method: 'POST', headers: { Authorization: `Bearer ${token}`, 'X-Registration-Bucket': race, 'X-Registration-Id': randomUUID() },
  });
  assert.equal(afterRestart.status, 429);
  // Global reservations are durable and atomic even under concurrent requests.
  const batch = await Promise.all(Array.from({ length: 60 }, (_, n) => restarted.fetch('https://tankai.internal/registration-limit', {
    method: 'POST', headers: { Authorization: `Bearer ${token}`,
      'X-Registration-Bucket': (n + 16).toString(16).padStart(2, '0') + '0'.repeat(30), 'X-Registration-Id': randomUUID() },
  })));
  assert.equal(batch.filter(response => response.status === 204).length, 41);
  assert.equal(batch.filter(response => response.status === 429).length, 19);
  assert.deepEqual(new Uint8Array(await (await call('GET')).arrayBuffer()), bytes);
  console.log('Auth state integration passed: access control, chunking, revision protection, durable restart.');
} finally {
  await runtime.dispose();
  await rm(directory, { recursive: true, force: true });
}

// Exercise the actual Worker fetch path with a controlled Durable Object standing
// in for the container. The bundled Worker code remains unchanged.
const forwardingDir = await mkdtemp(join(tmpdir(), 'tankai-forwarding-test-'));
const fixturePath = resolve('dist', `registration-forwarding-${randomUUID()}.mjs`);
const source = await readFile(resolve('dist/cloudflare.js'), 'utf8');
assert.ok(source.includes('  TankAIContainer,\n  cloudflare_default'), 'expected Worker exports');
await writeFile(fixturePath, source.replace("  TankAIContainer,\n  cloudflare_default", "  FakeRegistrationContainer as TankAIContainer,\n  cloudflare_default") + `
export class FakeRegistrationContainer extends DurableObject {
  async startAndWaitForPorts() {}
  async fetch(request) {
    const outcome = request.headers.get('X-Test-Outcome');
    if (outcome === 'throw') throw new Error('synthetic container error');
    return new Response(null, { status: Number(outcome) });
  }
}
`);
try {
  // Each case uses independent persisted storage, so limits from one case cannot
  // hide the outcome of another. All requests go through the real Worker.
  for (const outcome of ['400', '201', '503', 'throw']) {
    const scenarioDir = join(forwardingDir, outcome);
    const scenarioOptions = {
      ...options, scriptPath: fixturePath, resourcePersistencePath: scenarioDir,
      durableObjects: {
        ...options.durableObjects,
        TANKAI_CONTAINER: { className: 'TankAIContainer', useSQLite: true },
      },
    };
    const worker = new Miniflare(convertV4MiniflareOptions(scenarioOptions));
    try {
      const count = outcome === '400' ? 60 : 50;
      for (let n = 0; n < count; n++) {
        const response = await worker.dispatchFetch('https://tankaicore.com/api/auth/register', {
          method: 'POST', headers: { 'CF-Connecting-IP': `198.51.100.${n + 1}`, 'X-Test-Outcome': outcome },
        });
        assert.equal(response.status, outcome === 'throw' ? 503 : Number(outcome), `${outcome} request ${n}`);
      }
      const last = await worker.dispatchFetch('https://tankaicore.com/api/auth/register', {
        method: 'POST', headers: { 'CF-Connecting-IP': '203.0.113.250', 'X-Test-Outcome': '201' },
      });
      assert.equal(last.status, outcome === '400' ? 201 : 429, `daily quota after ${outcome}`);
    } finally {
      await worker.dispose();
    }
  }
  console.log('Registration forwarding passed: 400 releases; 201, 503 and thrown errors retain daily capacity.');
} finally {
  await rm(fixturePath, { force: true });
  await rm(forwardingDir, { recursive: true, force: true });
}
