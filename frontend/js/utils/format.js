export function formatDateTime(iso, locale) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(d);
}
export function formatTime(date, locale) {
  const d = date instanceof Date ? date : new Date(date);
  return Number.isNaN(d.getTime()) ? '—' : new Intl.DateTimeFormat(locale, { timeStyle: 'medium' }).format(d);
}
export function formatAuthors(authors, max = 3) {
  const list = Array.isArray(authors) ? authors.filter((a) => typeof a === 'string' && a.trim()) : [];
  if (!list.length) return '';
  return list.length > max ? `${list.slice(0, max).join(', ')} et al.` : list.join(', ');
}
export function formatBytes(n) {
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}
export function truncate(s, n) { s = String(s ?? ''); return s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s; }
export function pluralize(n, one, many = one + 's') { return `${n} ${n === 1 ? one : many}`; }
export function formatScore(x) { return typeof x === 'number' && Number.isFinite(x) ? x.toFixed(2) : null; }

/** Status presentation. Unknown statuses are shown verbatim (never hidden or guessed). */
const PAPER = {
  pending: ['Queued', 'info'], processing: ['Processing', 'info'], indexed: ['Indexed', 'ok'],
  failed: ['Failed', 'bad'], needs_ocr: ['Needs OCR', 'warn'],
};
const JOB = { pending: ['Pending', 'info'], running: ['Running', 'info'], succeeded: ['Succeeded', 'ok'], failed: ['Failed', 'bad'] };
export function statusMeta(kind, status) {
  const [label, tone] = (kind === 'job' ? JOB : PAPER)[status] ?? [status ? String(status) : 'Unknown', 'neutral'];
  return { label, tone };
}

/** Human text for backend warning codes (unknown codes are shown as-is). */
const WARNINGS = {
  invalid_citations_removed: 'The model cited sources that were not in the retrieved evidence; those citations were removed.',
  no_valid_citations: 'The answer contains no valid citations. Treat it as unverified.',
  evidence_truncated_to_fit_context: 'Some evidence was shortened to fit the model’s context window.',
};
export const describeWarning = (code) => WARNINGS[code] ?? String(code);
