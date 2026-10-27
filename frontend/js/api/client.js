import { getApiBase, TIMING } from '../config.js';

/** Error thrown for every failed request. `kind`: http | network | timeout | aborted | invalid_response. */
export class ApiError extends Error {
  constructor(message, { status = 0, code = 'error', details = null, kind = 'http' } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status; this.code = code; this.details = details; this.kind = kind;
  }
}

export function buildUrl(base, path, query) {
  const url = `${String(base ?? '').replace(/\/+$/, '')}${path.startsWith('/') ? path : `/${path}`}`;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v === undefined || v === null || v === '') continue;
    if (Array.isArray(v)) v.forEach((x) => params.append(k, String(x)));
    else params.append(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

function errorFromBody(res, body) {
  const e = body?.error;
  if (e && typeof e === 'object') {
    return new ApiError(e.message || `Request failed (${res.status})`, { status: res.status, code: e.code || 'error', details: e.details ?? null });
  }
  if (typeof body?.detail === 'string') return new ApiError(body.detail, { status: res.status });
  return new ApiError(`Request failed (${res.status}${res.statusText ? ` ${res.statusText}` : ''})`, { status: res.status });
}

/**
 * request(path, {method, query, json, formData, timeoutMs, signal, baseUrl, fetchImpl})
 * - JSON in/out; multipart via formData (the browser sets the Content-Type boundary: we never do).
 * - Resolves with parsed JSON (or null for empty bodies); rejects with ApiError. Never turns a failure into success.
 */
export async function request(path, opts = {}) {
  const { method = 'GET', query, json, formData, timeoutMs = TIMING.requestTimeoutMs, signal, baseUrl, fetchImpl } = opts;
  const doFetch = fetchImpl ?? globalThis.fetch;
  const url = buildUrl(baseUrl ?? getApiBase(), path, query);
  const headers = { Accept: 'application/json' };
  let body;
  if (formData !== undefined) body = formData;
  else if (json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(json); }

  const ctrl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, timeoutMs);
  const onAbort = () => ctrl.abort();
  if (signal) { if (signal.aborted) ctrl.abort(); else signal.addEventListener('abort', onAbort, { once: true }); }

  let res;
  try {
    res = await doFetch(url, { method, headers, body, signal: ctrl.signal });
  } catch (err) {
    if (err?.name === 'AbortError') {
      throw timedOut
        ? new ApiError('The request timed out.', { kind: 'timeout' })
        : new ApiError('The request was cancelled.', { kind: 'aborted' });
    }
    throw new ApiError('Cannot reach the server. Is the backend running and is the API base URL correct?', { kind: 'network' });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener?.('abort', onAbort);
  }

  const text = res.status === 204 ? '' : await res.text().catch(() => '');
  const isJson = /json/i.test(res.headers?.get?.('content-type') ?? '');
  let parsed = null;
  if (text) {
    if (isJson) {
      try { parsed = JSON.parse(text); }
      catch { throw new ApiError('The server returned malformed JSON.', { status: res.status, kind: 'invalid_response' }); }
    } else if (res.ok) {
      throw new ApiError('The server returned a non-JSON response. Check the API base URL (is it pointing at the backend?).', { status: res.status, kind: 'invalid_response' });
    }
  }
  if (!res.ok) throw errorFromBody(res, parsed);
  return parsed;
}

/** Short, user-facing description of an error. */
export function describeError(err) {
  if (!(err instanceof ApiError)) return 'Something went wrong.';
  if (err.kind === 'network') return err.message;
  if (err.status === 503 || err.status === 504) return `${err.message} The service may still be starting or a dependency is down; see System Status.`;
  return err.message;
}
