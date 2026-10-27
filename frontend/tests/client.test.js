import test from 'node:test';
import assert from 'node:assert/strict';
import { request, buildUrl, ApiError, describeError } from '../js/api/client.js';
import { normalizeApiBase, getApiBase } from '../js/config.js';
import { fakeFetch, jsonResponse, memoryStorage } from './helpers.js';

test('normalizeApiBase accepts same-origin and http(s) bases, rejects junk', () => {
  assert.equal(normalizeApiBase(''), ''); assert.equal(normalizeApiBase('  http://localhost:8000/ '), 'http://localhost:8000');
  assert.equal(normalizeApiBase('https://api.example.com/v1/'), 'https://api.example.com/v1');
  for (const bad of ['localhost:8000', 'javascript:alert(1)', 'ftp://x', 'http://a b']) assert.equal(normalizeApiBase(bad), null, bad);
});
test('getApiBase: default is same-origin, stored override wins, invalid stored value ignored', () => {
  assert.equal(getApiBase(memoryStorage()), '');
  assert.equal(getApiBase(memoryStorage({ 'ara.apiBase': 'http://localhost:8000/' })), 'http://localhost:8000');
  assert.equal(getApiBase(memoryStorage({ 'ara.apiBase': 'nonsense' })), '');
});
test('buildUrl joins base/path and skips empty query values', () => {
  assert.equal(buildUrl('', '/api/papers', { limit: 5, q: '', status: undefined, year: null }), '/api/papers?limit=5');
  assert.equal(buildUrl('http://h:1/', 'api/x'), 'http://h:1/api/x');
  assert.equal(buildUrl('', '/a', { ids: ['1', '2'] }), '/a?ids=1&ids=2');
});
test('success: parses JSON; JSON body sets content-type; GET has none', async () => {
  const f = fakeFetch(() => jsonResponse({ ok: 1 }));
  assert.deepEqual(await request('/api/x', { method: 'POST', json: { a: 1 }, fetchImpl: f, baseUrl: '' }), { ok: 1 });
  assert.equal(f.calls[0].init.headers['Content-Type'], 'application/json');
  assert.equal(f.calls[0].init.body, '{"a":1}');
  await request('/api/x', { fetchImpl: f, baseUrl: '' });
  assert.equal(f.calls[1].init.headers['Content-Type'], undefined);
});
test('multipart: FormData is sent as-is and Content-Type is NOT set manually', async () => {
  const f = fakeFetch(() => jsonResponse({}));
  const fd = new FormData(); fd.append('a', 'b');
  await request('/u', { method: 'POST', formData: fd, fetchImpl: f, baseUrl: '' });
  assert.equal(f.calls[0].init.body, fd);
  assert.equal(Object.keys(f.calls[0].init.headers).some((k) => k.toLowerCase() === 'content-type'), false);
});
test('backend error envelope -> ApiError with code, status, details', async () => {
  const f = fakeFetch(() => jsonResponse({ error: { code: 'validation_error', message: 'Invalid request.', details: [{ field: 'body.question', message: 'x' }] } }, 422));
  await assert.rejects(request('/api/chat', { fetchImpl: f, baseUrl: '' }), (e) => {
    assert.ok(e instanceof ApiError); assert.equal(e.status, 422); assert.equal(e.code, 'validation_error');
    assert.equal(e.message, 'Invalid request.'); assert.equal(e.details[0].field, 'body.question'); return true;
  });
});
test('FastAPI {detail} and bare HTTP errors are handled', async () => {
  await assert.rejects(request('/x', { fetchImpl: fakeFetch(() => jsonResponse({ detail: 'Not Found' }, 404)), baseUrl: '' }), (e) => e.message === 'Not Found' && e.status === 404);
  await assert.rejects(request('/x', { fetchImpl: fakeFetch(() => new Response('boom', { status: 500 })), baseUrl: '' }), (e) => e.status === 500 && /500/.test(e.message));
});
test('network failure -> kind network (never a fake success)', async () => {
  const f = fakeFetch(() => { throw new TypeError('Failed to fetch'); });
  await assert.rejects(request('/x', { fetchImpl: f, baseUrl: '' }), (e) => e.kind === 'network' && /Cannot reach/.test(describeError(e)));
});
test('timeout -> kind timeout; user abort -> kind aborted', async () => {
  const hang = (url, init) => new Promise((_, rej) => init.signal.addEventListener('abort', () => rej(Object.assign(new Error('a'), { name: 'AbortError' }))));
  await assert.rejects(request('/x', { fetchImpl: fakeFetch(hang), baseUrl: '', timeoutMs: 20 }), (e) => e.kind === 'timeout');
  const ac = new AbortController(); const p = request('/x', { fetchImpl: fakeFetch(hang), baseUrl: '', signal: ac.signal, timeoutMs: 5000 }); ac.abort();
  await assert.rejects(p, (e) => e.kind === 'aborted');
});
test('non-JSON 200 (e.g. HTML from a wrong base URL) is an error, malformed JSON too, empty/204 is null', async () => {
  await assert.rejects(request('/x', { fetchImpl: fakeFetch(() => new Response('<html></html>', { status: 200, headers: { 'content-type': 'text/html' } })), baseUrl: '' }), (e) => e.kind === 'invalid_response' && /API base URL/.test(e.message));
  await assert.rejects(request('/x', { fetchImpl: fakeFetch(() => jsonResponse('{oops')), baseUrl: '' }), (e) => e.kind === 'invalid_response');
  assert.equal(await request('/x', { fetchImpl: fakeFetch(() => new Response(null, { status: 204 })), baseUrl: '' }), null);
});
