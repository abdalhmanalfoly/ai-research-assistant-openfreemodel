import { mountShell } from '../components/sidebar.js';
import { el, clear, qs, skeletonRows } from '../utils/dom.js';
import { icon } from '../components/icons.js';
import { listJobs, isActive } from '../api/jobs.js';
import { describeError } from '../api/client.js';
import { formatDateTime, statusMeta, truncate } from '../utils/format.js';
import { TIMING } from '../config.js';

mountShell({ page: 'jobs', title: 'Processing Jobs' });
const root = qs('#page-content');
const state = { status: '', limit: 20, offset: 0, total: 0, items: [], source: 'server', error: null, loading: true };
let timer = null;

const status = el('select', { class: 'select', attrs: { id: 'jstatus' } }, [['', 'All'], ['pending', 'Pending'], ['running', 'Running'], ['succeeded', 'Succeeded'], ['failed', 'Failed']].map(([v, t]) => el('option', { value: v, text: t })));
const refresh = el('button', { class: 'btn', type: 'button' }, icon('refresh'), 'Refresh');
const note = el('div'); const body = el('div', { attrs: { 'aria-live': 'polite' } }); const pager = el('div', { class: 'pager' }); const live = el('span', { class: 'hint' });
root.replaceChildren(el('div', { class: 'stack' },
  el('div', { class: 'toolbar' }, el('div', { class: 'field' }, el('label', { text: 'Status', attrs: { for: 'jstatus' } }), status), refresh, live),
  note, body, pager,
  el('p', { class: 'hint', text: 'Jobs cannot be cancelled or retried from here: the backend offers no such actions. To retry a failed or OCR-needed paper, upload the file again; the backend then creates a new job for it.' })));

async function load({ quiet = false } = {}) {
  clearTimeout(timer);
  if (!quiet) { state.loading = true; paint(); }
  try { Object.assign(state, await listJobs({ limit: state.limit, offset: state.offset, status: state.status }), { error: null }); }
  catch (err) { state.error = describeError(err); state.items = []; }
  state.loading = false; paint();
  const active = state.items.some((j) => isActive(j.status));
  live.textContent = active ? `Auto-refreshing every ${TIMING.jobPollMs / 1000} s while jobs are active` : '';
  if (active && !state.error) timer = setTimeout(() => { if (!document.hidden) load({ quiet: true }); else timer = setTimeout(() => load({ quiet: true }), TIMING.jobPollMs); }, TIMING.jobPollMs);
}

function paint() {
  clear(body); clear(pager); clear(note);
  if (state.source === 'local') note.append(el('div', { class: 'callout callout--info' }, icon('info'), el('div', { text: 'This backend has no job list endpoint, so only jobs started from this browser are shown.' })));
  if (state.loading) { body.append(skeletonRows(4)); return; }
  if (state.error) { body.append(el('div', { class: 'callout callout--bad', attrs: { role: 'alert' } }, icon('alert'), el('div', {}, el('div', { text: state.error }), el('button', { class: 'btn btn--sm', type: 'button', text: 'Try again', on: { click: () => load() } })))); return; }
  if (!state.items.length) { body.append(el('div', { class: 'empty' }, icon('jobs'), el('h3', { text: state.status ? 'No jobs with this status' : 'No processing jobs yet' }), el('p', { text: 'Jobs appear here after you upload or import a paper.' }))); return; }
  body.append(el('div', { class: 'table-wrap' }, el('table', { class: 'table table--stack' },
    el('thead', {}, el('tr', {}, ['Job', 'Paper', 'Status', 'Created', 'Updated', 'Error'].map((h) => el('th', { text: h, attrs: { scope: 'col' } })))),
    el('tbody', {}, state.items.map((j) => {
      const sm = statusMeta('job', j.status);
      return el('tr', {},
        el('td', { attrs: { 'data-label': 'Job' } }, el('span', { class: 'mono small', text: j.id.slice(0, 8), attrs: { title: j.id } })),
        el('td', { class: 'title-cell', attrs: { 'data-label': 'Paper' } }, j.paperId ? el('a', { text: j.paperTitle ?? j.paperId, attrs: { href: `papers.html?paper=${encodeURIComponent(j.paperId)}` } }) : '—'),
        el('td', { attrs: { 'data-label': 'Status' } }, el('span', { class: `badge badge--${sm.tone}`, text: sm.label })),
        el('td', { text: formatDateTime(j.createdAt), attrs: { 'data-label': 'Created' } }), el('td', { text: formatDateTime(j.updatedAt), attrs: { 'data-label': 'Updated' } }),
        el('td', { class: 'small', text: j.errorMessage ? truncate(j.errorMessage, 160) : '—', attrs: { 'data-label': 'Error', title: j.errorMessage ?? '' } }));
    })))));
  if (state.source === 'server') {
    const from = state.offset + 1, to = state.offset + state.items.length;
    pager.append(el('span', { class: 'muted small', text: `Showing ${from}–${to} of ${state.total}` }), el('div', { class: 'row' },
      el('button', { class: 'btn btn--sm', type: 'button', disabled: state.offset === 0, on: { click: () => { state.offset = Math.max(0, state.offset - state.limit); load(); } } }, icon('left'), 'Previous'),
      el('button', { class: 'btn btn--sm', type: 'button', disabled: to >= state.total, on: { click: () => { state.offset += state.limit; load(); } } }, 'Next', icon('right'))));
  }
}
status.addEventListener('change', () => { state.status = status.value; state.offset = 0; load(); });
refresh.addEventListener('click', () => load());
globalThis.addEventListener?.('pagehide', () => clearTimeout(timer));
load();
