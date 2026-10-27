/* Non-secret frontend configuration. Browsers cannot read .env files, so this is a plain module.
 *
 * API base URL:
 *   ''  (default)  -> same origin: calls go to /api/... (frontend served by the FastAPI backend)
 *   'http://localhost:8000' -> backend on another origin (then the backend needs CORS_ORIGINS set)
 * It can be overridden per browser in Settings (stored in localStorage; never put secrets here).
 */
export const DEFAULT_API_BASE = '';

export const STORAGE_KEYS = {
  theme: 'ara.theme', sidebar: 'ara.sidebarCollapsed', apiBase: 'ara.apiBase',
  citationsExpanded: 'ara.citationsExpanded', chat: 'ara.chatHistory', jobs: 'ara.trackedJobs',
};

export const TIMING = {
  requestTimeoutMs: 30_000, chatTimeoutMs: 180_000, uploadTimeoutMs: 120_000,
  healthPollMs: 30_000, jobPollMs: 3_000,
};

export const LIMITS = { questionMax: 2000, defaultMaxPdfMb: 50, trackedJobsMax: 50, chatMessagesMax: 100 };

function safeStorage(kind) {
  try { return globalThis[kind] ?? null; } catch { return null; }
}

export function getPref(key, fallback = null, storage = safeStorage('localStorage')) {
  try { const v = storage?.getItem(key); return v === null || v === undefined ? fallback : v; } catch { return fallback; }
}
export function setPref(key, value, storage = safeStorage('localStorage')) {
  try { if (value === null || value === undefined) storage?.removeItem(key); else storage?.setItem(key, String(value)); return true; }
  catch { return false; }
}

/** Returns a normalised base ('' or 'http(s)://host[:port][/prefix]' without trailing slash) or null if invalid. */
export function normalizeApiBase(value) {
  const v = String(value ?? '').trim().replace(/\/+$/, '');
  if (v === '') return '';
  if (/^https?:\/\/[^\s/?#]+(\/[^\s?#]*)?$/i.test(v)) return v;
  return null;
}

export function getApiBase(storage = safeStorage('localStorage')) {
  const stored = normalizeApiBase(getPref(STORAGE_KEYS.apiBase, '', storage));
  return stored === null ? DEFAULT_API_BASE : (stored || DEFAULT_API_BASE);
}
