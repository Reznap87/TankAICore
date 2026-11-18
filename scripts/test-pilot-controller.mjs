// Real Miniflare SQLite Durable Objects, with explicit test-only native effects.
// No Cloudflare account, credentials, Docker or native snapshot calls.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

const directory = await mkdtemp(join(tmpdir(), 'tankai-controller-test-'));
assert.equal(directory.startsWith(join(tmpdir(), 'tankai-controller-test-')), true);
// workerd's Node-module fallback resolves against the project module root.
const fixtureDirectory = await mkdtemp(resolve('dist-pilot/test-fixture-'));
assert.equal(fixtureDirectory.startsWith(resolve('dist-pilot/test-fixture-')), true);
const scriptPath = join(fixtureDirectory, 'fixture.mjs');
await build({ entryPoints: ['scripts/fixtures/pilot-worker.ts'], outfile: scriptPath, bundle: true, format: 'esm',
  platform: 'node', target: 'es2022', external: ['cloudflare:workers', 'node:crypto'] });
const token = 's'.repeat(43);
let runtime;
let groups = 0;
const options = (key, scenario, selectedToken = token) => ({
  name: 'tankai-pilot-fixture', modules: true, scriptPath,
  compatibilityDate: '2026-10-07', compatibilityFlags: ['nodejs_compat'],
  bindings: { TEST_CASE: scenario, TOKEN: selectedToken },
  durableObjects: { TEST_PILOT: { className: 'TestPilot', useSQLite: true, unsafeUniqueKey: key } },
  resourcePersistencePath: directory,
});
async function open(key, scenario = 'normal', selectedToken = token) {
  if (runtime) await runtime.dispose();
  runtime = new Miniflare(convertV4MiniflareOptions(options(key, scenario, selectedToken)));
}
async function call(action = 'status', { id = randomUUID(), revision = 0, pin, authorization = `Bearer ${token}`,
  body, method = action === 'status' ? 'GET' : 'POST', headers = {} } = {}) {
  const response = await runtime.dispatchFetch('https://pilot.invalid/_pilot/' + action, {
    method, headers: { Authorization: authorization, 'If-Match': `"${revision}"`, 'Idempotency-Key': id,
      'Content-Type': 'application/json', ...headers },
    ...(method === 'GET' ? {} : { body: body ?? JSON.stringify(pin ? { pin } : {}) }),
  });
  const data = await response.json();
  assert.equal(response.headers.get('Cache-Control'), 'no-store');
  return { status: response.status, data, etag: response.headers.get('ETag') };
}
async function audit(role = 'source') {
  const response = await runtime.dispatchFetch('https://pilot.invalid/__test/audit', { headers: { 'X-Test-Role': role } });
  assert.equal(response.status, 200);
  return response.json();
}
async function seed(body) {
  const response = await runtime.dispatchFetch('https://pilot.invalid/__test/seed', { method: 'POST', body: JSON.stringify(body) });
  assert.equal(response.status, 200);
}
function count(audit, name) { return audit.events.filter(event => event.event === name).length; }
async function start() {
  const result = await call('start');
  assert.equal(result.status, 200, JSON.stringify(result));
  assert.equal(result.data.phase, 'running');
  assert.equal(result.etag, '"2"');
  return result;
}
async function capture() {
  const result = await call('capture', { revision: 2 });
  assert.equal(result.status, 200, JSON.stringify(result));
  assert.equal(result.data.phase, 'captured');
  assert.equal(result.data.revision, 4);
  assert.equal(JSON.stringify(result.data).includes('synthetic-native-handle'), false);
  return result;
}
async function check(name, run) {
  await run(); groups++;
  console.log('PASS ' + name);
}
try {
  await check('authorization and route rejection precede binding resolution', async () => {
    await open('edge-guard', 'reject-resolution');
    for (const authorization of ['Bearer wrong', 'Bearer ' + 'é'.repeat(43), '']) {
      assert.equal((await call('start', { authorization })).status, 403);
    }
    assert.equal((await call('unknown')).status, 404);
    assert.equal((await call('start', { method: 'GET' })).status, 405);
    assert.equal((await call('start', { headers: { 'If-Match': '' } })).status, 428);
    assert.equal((await call('start', { headers: { 'Idempotency-Key': 'invalid' } })).status, 400);
    assert.equal((await call('start', { headers: { 'Content-Type': 'text/plain' } })).status, 415);
    assert.equal((await call('start', { headers: { 'Content-Length': '600' }, body: 'x'.repeat(600) })).status, 413);
    await open('missing-token', 'reject-resolution', '');
    assert.equal((await call()).status, 403);
  });
  await check('cold status and strict request bodies do not start compute', async () => {
    await open('cold');
    assert.equal((await call()).data.phase, 'empty');
    for (const body of ['[]', 'null', 'bad', '{"image":"caller-selected"}', '{"snapshot_id":"foreign"}']) {
      assert.equal((await call('start', { body })).status >= 400, true);
    }
    assert.equal((await audit()).events.length, 0);
  });
  await check('immutable named image required before durable start intent', async () => {
    await open('unpinned', 'unpinned-image');
    assert.equal((await call('start')).status, 409);
    assert.equal((await audit()).state.phase, 'empty');
    assert.equal((await audit()).events.length, 0);
  });
  await check('concurrent and repeated requests have one native start', async () => {
    await open('concurrent');
    const id = randomUUID();
    const pending = call('start', { id });
    await new Promise(resolve => setTimeout(resolve, 15));
    const duplicate = await call('start', { id });
    assert.equal([200, 202].includes(duplicate.status), true);
    assert.equal((await call('start')).status, 409);
    assert.equal((await pending).status, 200);
    assert.equal((await call('start', { id })).status, 200);
    assert.equal((await call('capture', { id, revision: 2 })).status, 409);
    assert.equal(count(await audit(), 'start'), 1);
  });
  await check('capture pin and inspection survive durable runtime restart', async () => {
    await open('lifecycle'); await start(); const saved = await capture();
    const pin = saved.data.capsule.pin;
    await open('lifecycle');
    assert.equal((await call()).data.capsule.pin, pin);
    assert.equal((await call('restore', { revision: 4, pin: 'f'.repeat(64) })).status, 409);
    assert.equal((await audit('inspection')).events.length, 0);
    const id = randomUUID();
    const result = await call('restore', { id, revision: 4, pin });
    assert.equal(result.status, 200); assert.equal(result.data.phase, 'staged');
    assert.equal((await call('restore', { id, revision: 4, pin })).status, 200);
    assert.equal((await call('restore', { revision: 6, pin })).status, 409);
    assert.equal(count(await audit(), 'snapshot'), 1);
    assert.equal(count(await audit('inspection'), 'inspection-start'), 1);
    assert.equal((await audit('inspection')).state.receipt.child_running, false);
    assert.equal((await audit()).state.receipt.snapshot_ready, false);
    assert.equal((await call('inspection', { method: 'GET' })).data.phase, 'staged');
  });
  for (const scenario of ['expired', 'image-change']) await check(scenario + ' blocks before inspection effects', async () => {
    await open(scenario); await start(); const saved = await capture();
    await open(scenario, scenario);
    assert.equal((await call('restore', { revision: 4, pin: saved.data.capsule.pin })).status, 409);
    assert.equal((await audit()).state.phase, 'captured');
    assert.equal((await audit('inspection')).events.length, 0);
  });
  await check('capsule content tampering fails its independent pin', async () => {
    await open('tamper'); await start(); const saved = await capture();
    await seed({ corrupt: true });
    assert.equal((await call('restore', { revision: 5, pin: saved.data.capsule.pin })).status, 409);
    assert.equal((await audit('inspection')).events.length, 0);
  });
  await check('persisted pending capture never replays after eviction', async () => {
    await open('pending'); await start(); const id = randomUUID();
    await seed({ action: 'capture', id });
    await open('pending');
    assert.equal((await call()).data.recovery_required, true);
    assert.equal((await call('capture', { id, revision: 2 })).status, 202);
    assert.equal((await call('capture', { revision: 3 })).status, 409);
    assert.equal(count(await audit(), 'snapshot'), 0);
  });
  await check('concurrent restores use exactly one fresh inspection target', async () => {
    await open('concurrent-restore'); await start(); const saved = await capture();
    const results = await Promise.all([call('restore', { revision: 4, pin: saved.data.capsule.pin }),
      call('restore', { revision: 4, pin: saved.data.capsule.pin })]);
    assert.deepEqual(results.map(r => r.status).sort(), [200, 409]);
    assert.equal(count(await audit('inspection'), 'inspection-start'), 1);
  });
  await check('failed intent sync prevents start, capture and inspection effects', async () => {
    await open('sync-start', 'intent-sync-failure');
    assert.equal((await call('start')).status, 502);
    assert.equal((await audit()).events.length, 0);
    await open('sync-capture'); await start();
    await open('sync-capture', 'intent-sync-failure');
    assert.equal((await call('capture', { revision: 2 })).status, 502);
    assert.equal(count(await audit(), 'quiesce'), 0);
    await open('sync-inspector', 'inspection-sync-failure'); await start(); const saved = await capture();
    assert.equal((await call('restore', { revision: 4, pin: saved.data.capsule.pin })).status, 502);
    assert.equal((await audit('inspection')).events.length, 0);
  });
  for (const scenario of ['restore-finish-write-failure', 'inspection-write-failure']) await check(scenario + ' retains distributed intent across eviction', async () => {
    await open(scenario, scenario); await start(); const saved = await capture();
    const pin = saved.data.capsule.pin, id = randomUUID();
    assert.equal((await call('restore', { revision: 4, pin, id })).status >= 500, true);
    const parent = await audit(), target = await audit('inspection');
    assert.equal(parent.state.phase, scenario === 'restore-finish-write-failure' ? 'restoring' : 'blocked');
    assert.equal(target.state.phase, scenario === 'inspection-write-failure' ? 'restoring' : 'staged');
    assert.equal(count(target, 'inspection-start'), 1);
    await open(scenario);
    assert.equal([202, 409].includes((await call('restore', { revision: 4, pin, id })).status), true);
    assert.equal(count(await audit('inspection'), 'inspection-start'), 1);
    assert.equal((await call('inspection', { method: 'GET' })).data.phase, target.state.phase);
  });
  for (const scenario of ['start-error', 'snapshot-error', 'unquiesced', 'post-snapshot-change', 'finish-write-failure']) {
    await check(scenario + ' never claims successful capture or repeats unknown work', async () => {
      await open(scenario, scenario);
      const id = randomUUID();
      if (scenario !== 'start-error') await start();
      const action = scenario === 'start-error' ? 'start' : 'capture';
      assert.equal((await call(action, { id, revision: action === 'start' ? 0 : 2 })).status >= 500, true);
      const before = await audit();
      assert.equal(['blocked', 'capturing'].includes(before.state.phase), true);
      assert.equal(before.state.capsule, null);
      await open(scenario);
      assert.equal([202, 409].includes((await call(action, { id, revision: 0 })).status), true);
      assert.equal((await audit()).events.length, before.events.length);
      if (scenario === 'unquiesced') assert.equal(count(before, 'snapshot'), 0);
    });
  }
  for (const scenario of ['restore-error', 'inspect-mismatch']) await check(scenario + ' leaves source and target blocked from automatic activation', async () => {
    await open(scenario, scenario); await start(); const saved = await capture();
    assert.equal((await call('restore', { revision: 4, pin: saved.data.capsule.pin })).status, 502);
    assert.equal((await audit()).state.phase, 'blocked');
    assert.equal((await audit('inspection')).state.phase, 'blocked');
    assert.equal((await call('inspection', { method: 'GET' })).data.phase, 'blocked');
    assert.equal(count(await audit(), 'snapshot'), 1);
  });
  await check('actual native adapter uses fixed exec, offline starts and separate inspector', async () => {
    await open('native', 'native-adapter'); await start(); const saved = await capture();
    assert.equal((await call('restore', { revision: 4, pin: saved.data.capsule.pin })).status, 200);
    const source = await audit(), inspected = await audit('inspection');
    assert.equal(count(source, 'native-start'), 1);
    assert.equal(count(source, 'native-snapshot'), 1);
    assert.equal(count(inspected, 'native-start'), 1);
    const args = JSON.parse(inspected.events.find(e => e.event === 'native-start').details);
    assert.deepEqual(args.containerSnapshot, { id: 'synthetic-native-handle' });
    assert.equal(args.enableInternet, false);
    assert.equal(args.entrypoint.at(-1), 'inspect');
    assert.equal(Object.hasOwn(args, 'image'), false);
  });
  const artifact = await readFile(resolve('dist-pilot/cloudflare-pilot.js'), 'utf8');
  assert.equal(artifact.includes('/__test/'), false, 'test routes leaked into deploy artifact');
  assert.equal(artifact.includes('synthetic-native-handle'), false, 'native fixture leaked into deploy artifact');
  await check('deployable Worker itself rejects compute without a container binding', async () => {
    await runtime.dispose(); runtime = null;
    runtime = new Miniflare(convertV4MiniflareOptions({ name: 'actual-pilot-worker', modules: true,
      scriptPath: resolve('dist-pilot/cloudflare-pilot.js'), compatibilityDate: '2026-10-07', compatibilityFlags: ['nodejs_compat'],
      bindings: { TANKAI_PILOT_CONTROL_TOKEN: token }, resourcePersistencePath: directory,
      durableObjects: { TANKAI_PILOT: { className: 'TankAIPilot', useSQLite: true, unsafeUniqueKey: 'actual-pilot-cold' } } }));
    assert.equal((await call()).data.phase, 'empty');
    assert.equal((await call('inspection', { method: 'GET' })).data.phase, 'empty');
    assert.equal((await call('start')).status, 503);
    assert.equal((await call()).data.phase, 'empty');
  });
  console.log(`Pilot controller integration: ${groups} groups passed; real durable SQLite, fake native effects; no native Cloudflare snapshot proof.`);
} finally {
  if (runtime) await runtime.dispose();
  await rm(directory, { recursive: true, force: true });
  await rm(fixtureDirectory, { recursive: true, force: true });
}
