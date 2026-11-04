// Local-only operator tests. No account or remote service binding is contacted.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { request as httpRequest } from 'node:http';

const packageConfig = JSON.parse(await readFile('package.json', 'utf8'));
const operatorArguments = packageConfig.scripts['pilot:operator'].split(/\s+/);
assert.equal(operatorArguments.some(argument => /^(--local|-l)(=|$)/.test(argument)), false,
  'Explicit local mode disables the remote service binding');
const directory = await mkdtemp(resolve('dist-pilot/operator-test-'));
assert.equal(directory.startsWith(resolve('dist-pilot/operator-test-')), true);
const path = join(directory, 'operator.mjs');
let runtime;
const token = 'o'.repeat(43);
let calls = 0;
const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', 'If-Match': '"2"',
  'Idempotency-Key': '00000000-0000-4000-8000-000000000001' };
const request = (url = 'http://127.0.0.1:8766/_pilot/status', init = {}) => new Request(url, { headers, ...init });
let mode = 'normal';
async function downstream(req) {
  calls++;
  if (mode === 'html') return new Response('untrusted HTML', { headers: { 'Content-Type': 'text/html' } });
  if (mode === 'large') return Response.json({ large: 'x'.repeat(9000) });
  if (mode === 'redirect') return Response.json({ redirect: true }, { status: 302, headers: { Location: 'https://forbidden.invalid/' } });
  if (req.headers.get('Authorization') !== `Bearer ${token}`) return Response.json({ error: 'downstream_auth_denied' }, { status: 403 });
  return Response.json({ method: req.method, path: new URL(req.url).pathname, token_valid: true,
    cookie_seen: req.headers.has('Cookie'), diagnostic_seen: req.headers.has('X-Caller-Diagnostic'),
    redirect_mode: req.redirect, body: req.method === 'POST' ? await req.json() : null },
    { headers: { ETag: '"4"', 'Set-Cookie': 'must-not-pass=1', 'Access-Control-Allow-Origin': '*', 'X-Debug': 'must-not-pass' } });
}
try {
  await build({ entryPoints: ['src/pilot-operator.ts'], outfile: path, bundle: true, format: 'esm', platform: 'node', target: 'es2022', external: ['node:crypto'] });
  const { operatorFetch } = await import(pathToFileURL(path).href);
  const target = () => ({ fetch: downstream });
  for (const req of [request('https://evil.invalid/_pilot/status'), request('http://127.0.0.1:8767/_pilot/status'),
    request('https://127.0.0.1:8766/_pilot/status'), request(undefined, { headers: { ...headers, Origin: 'https://evil.invalid' } }),
    request(undefined, { headers: { ...headers, 'Sec-Fetch-Site': 'same-origin' } }), request(undefined, { headers: {} }),
    request(undefined, { headers: { ...headers, Authorization: 'Bearer bad' } })]) {
    assert.equal((await operatorFetch(req, target)).status, 403);
  }
  assert.equal(calls, 0, 'rejected callers resolved the service binding');
  assert.equal((await operatorFetch(request('http://127.0.0.1:8766/other'), target)).status, 404);
  assert.equal((await operatorFetch(request('http://127.0.0.1:8766/_pilot/status?target=other'), target)).status, 404);
  assert.equal((await operatorFetch(request('http://127.0.0.1:8766/_pilot/start', { method: 'POST', headers: { ...headers, 'Content-Length': '600' }, body: 'x'.repeat(600) }), target)).status, 413);
  assert.equal(calls, 0);
  const good = await operatorFetch(request('http://127.0.0.1:8766/_pilot/capture', { method: 'POST',
    headers: { ...headers, Cookie: 'private=1', 'X-Caller-Diagnostic': 'private' }, body: '{}' }), target);
  assert.equal(good.status, 200);
  assert.equal(good.headers.get('ETag'), '"4"');
  for (const name of ['Set-Cookie', 'Access-Control-Allow-Origin', 'X-Debug', 'Location']) assert.equal(good.headers.has(name), false);
  const data = await good.json();
  assert.deepEqual(data, { method: 'POST', path: '/_pilot/capture', token_valid: true, cookie_seen: false,
    diagnostic_seen: false, redirect_mode: 'manual', body: {} });
  assert.equal(JSON.stringify(data).includes(token), false);
  assert.equal((await operatorFetch(request(undefined, { headers: { ...headers, Authorization: `Bearer ${'w'.repeat(43)}` } }), target)).status, 403,
    'bridge mistook token syntax for downstream authentication');
  for (const scenario of ['html', 'large', 'redirect']) {
    mode = scenario;
    const result = await operatorFetch(request(), target);
    assert.equal(result.status, scenario === 'redirect' ? 302 : 502);
    assert.equal(result.headers.has('Location'), false);
  }
  mode = 'normal';
  runtime = new Miniflare(convertV4MiniflareOptions({ name: 'pilot-operator-runtime', modules: true, scriptPath: path,
    compatibilityDate: '2026-10-07', compatibilityFlags: ['nodejs_compat'], serviceBindings: { PILOT: downstream } }));
  const before = calls;
  const ready = await runtime.ready;
  async function actualHttp(extra = {}) {
    return new Promise((resolveResult, reject) => {
      const req = httpRequest(new URL('/_pilot/status', ready), { headers: { ...headers, Host: '127.0.0.1:8766', ...extra } }, res => {
        res.resume(); res.on('end', () => resolveResult(res.statusCode));
      });
      req.on('error', reject); req.end();
    });
  }
  assert.equal(await actualHttp(), 200);
  assert.equal(calls, before + 1);
  assert.equal(await actualHttp({ Origin: 'null' }), 403);
  assert.equal(calls, before + 1);
  console.log('Operator bridge passed: loopback/browser guards, fixed routes, downstream authentication, header filtering, bounded bodies, manual redirects and actual workerd service binding with a local fake.');
} finally {
  if (runtime) await runtime.dispose();
  await rm(directory, { recursive: true, force: true });
}
