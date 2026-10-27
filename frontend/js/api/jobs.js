import { request, ApiError } from './client.js';
import { STORAGE_KEYS, LIMITS, getPref, setPref } from '../config.js';

const str = (v) => (typeof v === 'string' && v ? v : null);
export const isActive = (s) => s === 'pending' || s === 'running';
export const isTerminal = (s) => s === 'succeeded' || s === 'failed';

export function normalizeJob(j = {}) {
  return { id: String(j.id ?? ''), paperId: str(j.paper_id), paperTitle: str(j.paper_title), status: str(j.status) ?? 'unknown',
    errorMessage: str(j.error_message), createdAt: str(j.created_at), updatedAt: str(j.updated_at) };
}

export async function getJob(id, opts = {}) {
  const raw = await request(`/api/jobs/${encodeURIComponent(id)}`, opts);
  if (!raw || typeof raw !== 'object') throw new ApiError('Unexpected response from the jobs API.', { kind: 'invalid_response' });
  return normalizeJob(raw);
}

/* Jobs started from this browser are remembered (ids only) so they can be followed even on a backend without GET /api/jobs. */
export function getTrackedJobIds(storage) {
  try { const v = JSON.parse(getPref(STORAGE_KEYS.jobs, '[]', storage)); return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []; } catch { return []; }
}
export function trackJob(id, storage) {
  const ids = [id, ...getTrackedJobIds(storage).filter((x) => x !== id)].slice(0, LIMITS.trackedJobsMax);
  setPref(STORAGE_KEYS.jobs, JSON.stringify(ids), storage);
}

/** Lists jobs. Uses GET /api/jobs; if the backend predates it (404/405) falls back to the ids tracked in this browser. */
export async function listJobs({ limit = 20, offset = 0, status } = {}, opts = {}) {
  try {
    const raw = await request('/api/jobs', { query: { limit, offset, status }, ...opts });
    if (!raw || !Array.isArray(raw.items)) throw new ApiError('Unexpected response from the jobs API.', { kind: 'invalid_response' });
    return { items: raw.items.map(normalizeJob), total: Number.isInteger(raw.total) ? raw.total : raw.items.length, limit, offset, source: 'server' };
  } catch (err) {
    if (!(err instanceof ApiError) || ![404, 405].includes(err.status)) throw err;
    const ids = getTrackedJobIds();
    const settled = await Promise.allSettled(ids.map((id) => getJob(id, opts)));
    const items = settled.filter((r) => r.status === 'fulfilled').map((r) => r.value).filter((j) => !status || j.status === status);
    return { items, total: items.length, limit, offset: 0, source: 'local' };
  }
}
