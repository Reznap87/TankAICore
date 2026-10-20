// Integration test against the actual bundled Worker and Durable Object runtime.
// Run after npm run cf:check. No production resources or credentials are used.
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const directory = await mkdtemp(join(tmpdir(), 'tankai-auth-test-'));
const token = 'a'.repeat(43);
const options = {
  name: 'tankai-auth-test',
  modules: true, scriptPath: resolve('dist/cloudflare.js'),
  compatibilityDate: '2026-08-20', compatibilityFlags: ['nodejs_compat'],
  bindings: { TANKAI_AUTH_STATE_TOKEN: token },
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
  console.log('Auth state integration passed: access control, chunking, revision protection, durable restart.');
} finally {
  await runtime.dispose();
  await rm(directory, { recursive: true, force: true });
}
