import { request, ApiError } from './client.js';

export async function getHealth(opts = {}) {
  const raw = await request('/api/health', opts);
  if (!raw || typeof raw !== 'object') throw new ApiError('Unexpected response from the health API.', { kind: 'invalid_response' });
  return raw;
}

const has = (v) => v !== null && v !== undefined;

/** Turns the raw /api/health payload into display rows. Never claims health that the payload does not state. */
export function describeHealth(raw = {}) {
  const c = [];
  const add = (key, label, state, detail) => c.push({ key, label, state, detail });
  add('api', 'API', 'ok', 'Reachable. This only means the web server answered; see the other checks for readiness.');

  // The backend reports CONFIGURATION here (it never calls a model from /api/health). Say exactly that.
  const k = raw.llm;
  add('openrouter', 'OpenRouter API key', k === 'configured' ? 'ok' : k === 'not_configured' ? 'bad' : 'unknown',
    k === 'configured' ? 'Configured on the server (not verified here; use the provider check to test it)'
      : k === 'not_configured' ? 'Not configured. Set OPENROUTER_API_KEY in the backend .env and restart.' : 'Not reported');
  add('chat_model', 'Chat model', k === 'configured' ? 'ok' : k === 'not_configured' ? 'bad' : 'unknown',
    `${raw.chat_model_name ?? 'unknown model'}${raw.llm_provider ? ' · ' + raw.llm_provider : ''} — ${k === 'configured' ? 'configured' : k === 'not_configured' ? 'cannot be used without an API key' : 'status unknown'}`);

  const em = raw.embedding;
  const emBits = [raw.embedding_model_name, raw.embedding_provider, has(raw.embedding_dimension) ? `${raw.embedding_dimension} dims` : null].filter(Boolean);
  add('embedding', 'Embedding model', em === 'configured' ? 'ok' : em === 'not_configured' ? 'bad' : 'unknown',
    `${emBits.join(' · ') || 'unknown'} — ${em === 'configured' ? 'configured' : em === 'not_configured' ? 'cannot be used without an API key' : 'status not reported'}`);

  const db = raw.database;
  add('database', 'Database', db === 'up' ? 'ok' : db === 'down' ? 'bad' : 'unknown', db === 'up' ? 'Connected' : db === 'down' ? 'Not reachable' : 'Not reported');
  const pv = raw.pgvector;
  add('pgvector', 'pgvector extension', pv === 'installed' ? 'ok' : pv === 'missing' ? 'bad' : 'unknown', pv ?? 'Not reported');

  if (!has(raw.schema_dimension)) add('schema', 'Vector column size', 'unknown', 'Unknown (tables missing or not migrated yet?)');
  else if (raw.schema_dimension === raw.embedding_dimension) add('schema', 'Vector column size', 'ok', `${raw.schema_dimension} — matches the embedding model`);
  else add('schema', 'Vector column size', 'bad', `Database column is ${raw.schema_dimension}, model produces ${raw.embedding_dimension ?? '?'}. Run migrations.`);

  const p = raw.chunks_pending_reindex;
  add('reindex', 'Chunks pending re-index', !has(p) ? 'unknown' : p === 0 ? 'ok' : 'warn',
    !has(p) ? 'Unknown' : p === 0 ? 'None' : `${p} chunk(s) are not searchable until re-indexing finishes`);

  const bad = c.some((x) => x.state === 'bad'), warn = c.some((x) => x.state === 'warn');
  const overall = raw.status === 'ok' && !bad && !warn ? 'ready' : 'degraded';
  return { overall, components: c, maxPdfMb: Number.isInteger(raw.max_pdf_mb) ? raw.max_pdf_mb : null };
}
