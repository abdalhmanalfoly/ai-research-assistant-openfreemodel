import { request, ApiError } from './client.js';
import { TIMING } from '../config.js';

const str = (v) => (typeof v === 'string' && v ? v : null);
export function normalizePaper(p = {}) {
  return {
    id: String(p.id ?? ''), title: str(p.title) ?? 'Untitled paper',
    authors: Array.isArray(p.authors) ? p.authors.filter((a) => typeof a === 'string') : [],
    abstract: str(p.abstract), year: Number.isInteger(p.publication_year) ? p.publication_year : null,
    doi: str(p.doi), sourceUrl: str(p.source_url), status: str(p.status) ?? 'unknown', errorMessage: str(p.error_message),
    createdAt: str(p.created_at), updatedAt: str(p.updated_at),
  };
}

export async function listPapers({ limit = 20, offset = 0, q, status, year } = {}, opts = {}) {
  const raw = await request('/api/papers', { query: { limit, offset, q: q?.trim(), status, year }, ...opts });
  if (!raw || !Array.isArray(raw.items)) throw new ApiError('Unexpected response from the papers API.', { kind: 'invalid_response' });
  return { items: raw.items.map(normalizePaper), total: Number.isInteger(raw.total) ? raw.total : raw.items.length, limit: raw.limit ?? limit, offset: raw.offset ?? offset };
}

export async function getPaper(id, opts = {}) {
  const raw = await request(`/api/papers/${encodeURIComponent(id)}`, opts);
  if (!raw || typeof raw !== 'object') throw new ApiError('Unexpected response from the papers API.', { kind: 'invalid_response' });
  return normalizePaper(raw);
}

export function buildUploadForm({ file, title, authors, year, doi, sourceUrl }) {
  const fd = new FormData();
  fd.append('file', file, file.name);
  const add = (k, v) => { if (v !== undefined && v !== null && String(v).trim() !== '') fd.append(k, String(v).trim()); };
  add('title', title); add('authors', authors); add('year', year); add('doi', doi); add('source_url', sourceUrl);
  return fd;
}

export async function uploadPaper(params, { signal, baseUrl, fetchImpl } = {}) {
  const raw = await request('/api/papers/upload', { method: 'POST', formData: buildUploadForm(params), timeoutMs: TIMING.uploadTimeoutMs, signal, baseUrl, fetchImpl });
  if (!raw || typeof raw.paper_id !== 'string') throw new ApiError('Unexpected response from the upload API.', { kind: 'invalid_response' });
  return { paperId: raw.paper_id, jobId: str(raw.job_id), alreadyRegistered: raw.already_registered === true };
}
