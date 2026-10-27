import test from 'node:test';
import assert from 'node:assert/strict';
import { askQuestion, buildChatBody, normalizeChatResponse } from '../js/api/chat.js';
import { listPapers, uploadPaper, normalizePaper, buildUploadForm } from '../js/api/papers.js';
import { listJobs, normalizeJob, trackJob, getTrackedJobIds, isActive, isTerminal } from '../js/api/jobs.js';
import { describeHealth } from '../js/api/health.js';
import { fakeFetch, jsonResponse, memoryStorage } from './helpers.js';

test('buildChatBody sends only what is set', () => {
  assert.deepEqual(buildChatBody({ question: 'q' }), { question: 'q' });
  assert.deepEqual(buildChatBody({ question: 'q', paperIds: ['a'], topK: 3, yearMin: 2020, yearMax: undefined }), { question: 'q', paper_ids: ['a'], top_k: 3, year_min: 2020 });
});
test('askQuestion posts to /api/chat and normalises (null/missing fields tolerated)', async () => {
  const f = fakeFetch(() => jsonResponse({ answer: 'A [E1]', citations: [{ id: 'E1', paper_id: 'p', title: null, authors: null, page_number: null, section: null, doi: null, source_url: null, relevance_score: 0.61234, excerpt: null }], retrieved_chunks: 3, model: 'qwen2.5:3b', insufficient_evidence: false, warnings: null }));
  const r = await askQuestion({ question: 'hello' }, { fetchImpl: f, baseUrl: '' });
  assert.equal(f.calls[0].url, '/api/chat'); assert.equal(f.calls[0].init.method, 'POST');
  assert.equal(r.citations[0].title, 'Untitled paper'); assert.equal(r.citations[0].pageNumber, null); assert.deepEqual(r.warnings, []);
  assert.equal(r.insufficientEvidence, false); assert.equal(r.retrievedChunks, 3);
});
test('normalizeChatResponse: no citations stays empty (nothing is invented); bad payload rejected', () => {
  const r = normalizeChatResponse({ answer: 'x', citations: [], insufficient_evidence: true });
  assert.deepEqual(r.citations, []); assert.equal(r.insufficientEvidence, true);
  assert.throws(() => normalizeChatResponse(null)); assert.throws(() => normalizeChatResponse({ foo: 1 }));
});
test('listPapers: empty library, query params, nullable fields', async () => {
  const f = fakeFetch(() => jsonResponse({ items: [], total: 0, limit: 20, offset: 0 }));
  const r = await listPapers({ limit: 20, offset: 40, q: ' retina ', status: '', year: undefined }, { fetchImpl: f, baseUrl: '' });
  assert.equal(f.calls[0].url, '/api/papers?limit=20&offset=40&q=retina'); assert.deepEqual(r.items, []); assert.equal(r.total, 0);
  const p = normalizePaper({ id: 'x', title: null, authors: null, abstract: null, publication_year: null, doi: null, source_url: null, status: 'indexed' });
  assert.equal(p.title, 'Untitled paper'); assert.equal(p.year, null); assert.deepEqual(p.authors, []);
  assert.equal(normalizePaper(undefined).title, 'Untitled paper');
});
test('upload: multipart fields, only non-empty metadata, success and already_registered', async () => {
  const file = new File([new Uint8Array([37, 80, 68, 70])], 'p.pdf', { type: 'application/pdf' });
  const fd = buildUploadForm({ file, title: ' T ', authors: '', year: '2021', doi: undefined, sourceUrl: '' });
  assert.deepEqual([...fd.keys()].sort(), ['file', 'title', 'year']); assert.equal(fd.get('title'), 'T');
  const f = fakeFetch(() => jsonResponse({ paper_id: 'p1', job_id: 'j1', already_registered: false }, 202));
  assert.deepEqual(await uploadPaper({ file }, { fetchImpl: f, baseUrl: '' }), { paperId: 'p1', jobId: 'j1', alreadyRegistered: false });
  const dup = fakeFetch(() => jsonResponse({ paper_id: 'p1', job_id: null, already_registered: true }, 202));
  assert.deepEqual(await uploadPaper({ file }, { fetchImpl: dup, baseUrl: '' }), { paperId: 'p1', jobId: null, alreadyRegistered: true });
});
test('upload failure surfaces backend error (no fake success)', async () => {
  const file = new File(['x'], 'p.pdf', { type: 'application/pdf' });
  const f = fakeFetch(() => jsonResponse({ error: { code: 'invalid_upload', message: 'File is not a PDF.' } }, 400));
  await assert.rejects(uploadPaper({ file }, { fetchImpl: f, baseUrl: '' }), (e) => e.code === 'invalid_upload' && e.status === 400);
});
test('jobs: normalisation, status helpers, server list', async () => {
  const j = normalizeJob({ id: 'j', paper_id: 'p', paper_title: null, status: 'running', error_message: null, created_at: '2025-01-01T00:00:00Z', updated_at: null });
  assert.equal(j.paperTitle, null); assert.ok(isActive(j.status)); assert.ok(isTerminal('failed')); assert.ok(!isTerminal('running'));
  const f = fakeFetch(() => jsonResponse({ items: [{ id: 'a', paper_id: 'p', status: 'succeeded' }], total: 1 }));
  const r = await listJobs({}, { fetchImpl: f, baseUrl: '' });
  assert.equal(r.source, 'server'); assert.equal(r.items[0].status, 'succeeded');
});
test('jobs: falls back to browser-tracked ids when GET /api/jobs does not exist (404)', async () => {
  globalThis.localStorage = memoryStorage(); trackJob('job-1'); trackJob('job-2'); trackJob('job-1');
  assert.deepEqual(getTrackedJobIds(), ['job-1', 'job-2']);
  const f = fakeFetch((url) => url === '/api/jobs?limit=20&offset=0' ? jsonResponse({ detail: 'Not Found' }, 404) : jsonResponse({ id: url.split('/').pop(), paper_id: 'p', status: 'pending' }));
  const r = await listJobs({}, { fetchImpl: f, baseUrl: '' });
  assert.equal(r.source, 'local'); assert.deepEqual(r.items.map((x) => x.id), ['job-1', 'job-2']);
  const f500 = fakeFetch(() => jsonResponse({ error: { code: 'database_error', message: 'x' } }, 503));
  await assert.rejects(listJobs({}, { fetchImpl: f500, baseUrl: '' }), (e) => e.status === 503); // other errors are NOT masked
  delete globalThis.localStorage;
});
test('describeHealth: ready only when everything is positively ok', () => {
  const ok = { status: 'ok', llm_provider: 'openrouter', llm: 'configured', embedding: 'configured', embedding_provider: 'openrouter', embedding_dimension: 1024, database: 'up', pgvector: 'installed', schema_dimension: 1024, chunks_pending_reindex: 0, chat_model_name: 'nvidia/nemotron-3.5-super-vl:free', embedding_model_name: 'nvidia/nemotron-3.5-embed-1b:free', max_pdf_mb: 50 };
  const d = describeHealth(ok); assert.equal(d.overall, 'ready'); assert.equal(d.maxPdfMb, 50); assert.ok(d.components.every((c) => c.state === 'ok'));
  const bad = describeHealth({ ...ok, status: 'degraded', llm: 'not_configured', embedding: 'not_configured', schema_dimension: 1536, chunks_pending_reindex: 12 });
  const by = Object.fromEntries(bad.components.map((c) => [c.key, c.state]));
  assert.equal(bad.overall, 'degraded'); assert.equal(by.api, 'ok'); assert.equal(by.openrouter, 'bad'); assert.equal(by.embedding, 'bad'); assert.equal(by.schema, 'bad'); assert.equal(by.reindex, 'warn');
});
test('describeHealth: missing/null fields are "unknown", never "ok"; API-up alone is not ready', () => {
  const d = describeHealth({ status: 'degraded', schema_dimension: null, chunks_pending_reindex: null });
  const by = Object.fromEntries(d.components.map((c) => [c.key, c.state]));
  assert.equal(d.overall, 'degraded'); assert.equal(by.openrouter, 'unknown'); assert.equal(by.database, 'unknown'); assert.equal(by.schema, 'unknown'); assert.equal(d.maxPdfMb, null);
});
