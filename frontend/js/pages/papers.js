import { mountShell } from '../components/sidebar.js';
import { el, clear, debounce, qs, skeletonRows, copyText } from '../utils/dom.js';
import { icon } from '../components/icons.js';
import { openModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { listPapers, getPaper } from '../api/papers.js';
import { describeError, ApiError } from '../api/client.js';
import { formatAuthors, formatDateTime, statusMeta } from '../utils/format.js';
import { doiUrl, safeHttpUrl } from '../utils/validation.js';

const { actions } = mountShell({ page: 'papers', title: 'Research Library' });
const root = qs('#page-content');
const state = { q: '', status: '', year: '', limit: 20, offset: 0, total: 0, items: [], loading: false, error: null };
let controller = null;

const search = el('input', { class: 'input', type: 'search', attrs: { id: 'q', placeholder: 'Search by title…', 'aria-label': 'Search by title' } });
const status = el('select', { class: 'select', attrs: { id: 'status' } }, [['', 'All statuses'], ['indexed', 'Indexed'], ['pending', 'Queued'], ['processing', 'Processing'], ['failed', 'Failed'], ['needs_ocr', 'Needs OCR']].map(([v, t]) => el('option', { value: v, text: t })));
const year = el('input', { class: 'input', type: 'number', attrs: { id: 'year', min: 1000, max: 3000, placeholder: 'Any' } });
const size = el('select', { class: 'select', attrs: { id: 'size' } }, [10, 20, 50, 100].map((n) => el('option', { value: String(n), text: `${n} per page`, selected: n === 20 })));
const refresh = el('button', { class: 'btn', type: 'button' }, icon('refresh'), 'Refresh');
const body = el('div', { attrs: { 'aria-live': 'polite' } });
const pager = el('div', { class: 'pager' });

root.replaceChildren(el('div', { class: 'stack' },
  el('div', { class: 'toolbar' },
    el('div', { class: 'field field--grow' }, el('label', { text: 'Title search', attrs: { for: 'q' } }), search),
    el('div', { class: 'field' }, el('label', { text: 'Status', attrs: { for: 'status' } }), status),
    el('div', { class: 'field' }, el('label', { text: 'Year', attrs: { for: 'year' } }), year),
    el('div', { class: 'field' }, el('label', { text: 'Page size', attrs: { for: 'size' } }), size), refresh),
  el('p', { class: 'hint', text: 'Search, status and year filters are applied by the server across the whole library (search matches the title only).' }),
  body, pager));

async function load() {
  controller?.abort(); controller = new AbortController();
  state.loading = true; state.error = null; paint();
  try {
    const r = await listPapers({ limit: state.limit, offset: state.offset, q: state.q, status: state.status, year: state.year }, { signal: controller.signal });
    Object.assign(state, { items: r.items, total: r.total });
    if (!r.items.length && r.total > 0 && state.offset > 0) { state.offset = 0; return load(); }
  } catch (err) { if (err instanceof ApiError && err.kind === 'aborted') return; state.error = describeError(err); state.items = []; }
  state.loading = false; paint();
}

function row(p) {
  const d = doiUrl(p.doi); const sm = statusMeta('paper', p.status);
  return el('tr', {},
    el('td', { class: 'title-cell', dataset: { label: 'Title' } }, el('button', { class: 'link-btn', type: 'button', text: p.title, on: { click: () => showDetails(p.id) } })),
    el('td', { text: formatAuthors(p.authors) || '—', attrs: { 'data-label': 'Authors' } }),
    el('td', { text: p.year ?? '—', attrs: { 'data-label': 'Year' } }),
    el('td', { attrs: { 'data-label': 'Status' } }, el('span', { class: `badge badge--${sm.tone}`, text: sm.label })),
    el('td', { attrs: { 'data-label': 'DOI' } }, d ? el('a', { text: p.doi, attrs: { href: d, target: '_blank', rel: 'noopener noreferrer' } }) : '—'),
    el('td', { text: formatDateTime(p.createdAt), attrs: { 'data-label': 'Added' } }));
}

function paint() {
  clear(body); clear(pager);
  if (state.loading) { body.append(skeletonRows(5)); return; }
  if (state.error) {
    body.append(el('div', { class: 'callout callout--bad', attrs: { role: 'alert' } }, icon('alert'), el('div', {}, el('div', { text: state.error }), el('button', { class: 'btn btn--sm', type: 'button', text: 'Try again', on: { click: load } }))));
    return;
  }
  if (!state.items.length) {
    const filtered = state.q || state.status || state.year;
    body.append(el('div', { class: 'empty' }, icon('library'), el('h3', { text: filtered ? 'No papers match these filters' : 'The library is empty' }),
      el('p', { text: filtered ? 'Try clearing the search or filters.' : 'No papers have been indexed yet.' }),
      filtered ? null : el('a', { class: 'btn btn--primary', href: 'upload.html', text: 'Upload a paper' })));
    return;
  }
  body.append(el('div', { class: 'table-wrap' }, el('table', { class: 'table table--stack' },
    el('thead', {}, el('tr', {}, ['Title', 'Authors', 'Year', 'Status', 'DOI', 'Added'].map((h) => el('th', { text: h, attrs: { scope: 'col' } })))),
    el('tbody', {}, state.items.map(row)))));
  const from = state.offset + 1, to = state.offset + state.items.length;
  const prev = el('button', { class: 'btn btn--sm', type: 'button', disabled: state.offset === 0, on: { click: () => { state.offset = Math.max(0, state.offset - state.limit); load(); } } }, icon('left'), 'Previous');
  const next = el('button', { class: 'btn btn--sm', type: 'button', disabled: to >= state.total, on: { click: () => { state.offset += state.limit; load(); } } }, 'Next', icon('right'));
  pager.append(el('span', { class: 'muted small', text: `Showing ${from}–${to} of ${state.total}` }), el('div', { class: 'row' }, prev, next));
}

async function showDetails(id) {
  const content = el('div', { class: 'stack' }, skeletonRows(3));
  const m = openModal({ title: 'Paper details', body: content, onClose: () => { const u = new URL(location.href); u.searchParams.delete('paper'); history.replaceState(null, '', u); } });
  try {
    const p = await getPaper(id);
    const sm = statusMeta('paper', p.status);
    const link = (url, label) => (url ? el('a', { text: label ?? url, attrs: { href: url, target: '_blank', rel: 'noopener noreferrer' } }) : '—');
    const copy = el('button', { class: 'link-btn', type: 'button', text: 'Copy', on: { click: async () => toast((await copyText(p.id)) ? 'ID copied' : 'Could not copy') } });
    clear(content).append(
      el('h3', { text: p.title }),
      p.errorMessage ? el('div', { class: 'callout callout--bad' }, icon('alert'), el('div', { text: p.errorMessage })) : null,
      el('dl', { class: 'dl' },
        el('dt', { text: 'Status' }), el('dd', {}, el('span', { class: `badge badge--${sm.tone}`, text: sm.label })),
        el('dt', { text: 'Authors' }), el('dd', { text: p.authors.join(', ') || '—' }),
        el('dt', { text: 'Year' }), el('dd', { text: p.year ?? '—' }),
        el('dt', { text: 'DOI' }), el('dd', {}, link(doiUrl(p.doi), p.doi)),
        el('dt', { text: 'Source' }), el('dd', {}, link(safeHttpUrl(p.sourceUrl))),
        el('dt', { text: 'Paper ID' }), el('dd', {}, el('span', { class: 'mono', text: p.id }), ' ', copy),
        el('dt', { text: 'Added' }), el('dd', { text: formatDateTime(p.createdAt) }),
        el('dt', { text: 'Updated' }), el('dd', { text: formatDateTime(p.updatedAt) })),
      p.abstract ? el('div', {}, el('div', { class: 'label', text: 'Abstract' }), el('p', { text: p.abstract })) : el('p', { class: 'muted small', text: 'No abstract stored for this paper.' }),
      el('div', { class: 'row' }, p.status === 'indexed' ? el('a', { class: 'btn btn--primary btn--sm', href: `chat.html?paper=${encodeURIComponent(p.id)}`, text: 'Ask about this paper' }) : null),
      el('p', { class: 'hint', text: 'The original PDF cannot be opened from here: the backend does not expose a file endpoint.' }));
  } catch (err) { clear(content).append(el('div', { class: 'callout callout--bad', attrs: { role: 'alert' } }, icon('alert'), el('div', { text: describeError(err) }))); }
  return m;
}

const onSearch = debounce(() => { state.q = search.value; state.offset = 0; load(); }, 350);
search.addEventListener('input', onSearch);
status.addEventListener('change', () => { state.status = status.value; state.offset = 0; load(); });
year.addEventListener('change', () => { state.year = year.value; state.offset = 0; load(); });
size.addEventListener('change', () => { state.limit = Number(size.value); state.offset = 0; load(); });
refresh.addEventListener('click', load);

const deep = new URLSearchParams(location.search).get('paper');
load();
if (deep) showDetails(deep);
