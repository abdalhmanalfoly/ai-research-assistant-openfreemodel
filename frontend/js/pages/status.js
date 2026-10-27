import { mountShell } from '../components/sidebar.js';
import { el, clear, qs, skeletonRows } from '../utils/dom.js';
import { icon } from '../components/icons.js';
import { getHealth, describeHealth } from '../api/health.js';
import { describeError } from '../api/client.js';
import { formatTime } from '../utils/format.js';
import { TIMING } from '../config.js';

mountShell({ page: 'status', title: 'System Status' });
const root = qs('#page-content');
const banner = el('div'); const grid = el('div', { class: 'status-grid' }); const stamp = el('span', { class: 'hint' });
const refresh = el('button', { class: 'btn', type: 'button' }, icon('refresh'), 'Refresh now');
const auto = el('input', { type: 'checkbox', checked: true, attrs: { id: 'auto' } });
root.replaceChildren(el('div', { class: 'stack' }, banner, el('div', { class: 'row' }, refresh, el('label', { class: 'switch small', attrs: { for: 'auto' } }, auto, `Auto-refresh every ${TIMING.healthPollMs / 1000} s`), stamp), grid,
  el('p', { class: 'hint', text: 'These checks never load the embedding model. “Reachable” only means the web server answered; the other rows show whether each dependency is actually ready.' })));

let lastOk = null, timer = null;
const dotFor = { ok: 'dot--ok', warn: 'dot--warn', bad: 'dot--bad', unknown: '' };
const wordFor = { ok: 'OK', warn: 'Attention', bad: 'Problem', unknown: 'Unknown' };

async function load() {
  clearTimeout(timer);
  if (!grid.children.length) grid.append(skeletonRows(3));
  try {
    const d = describeHealth(await getHealth({ timeoutMs: 10000 }));
    lastOk = new Date();
    clear(banner).append(d.overall === 'ready'
      ? el('div', { class: 'callout callout--ok' }, icon('check'), el('div', { text: 'Ready: every reported check passes.' }))
      : el('div', { class: 'callout callout--warn' }, icon('alert'), el('div', { text: 'Degraded: the API answers, but at least one dependency is not fully ready. Questions or uploads may fail.' })));
    clear(grid).append(...d.components.map((c) => el('div', { class: 'card status-card' },
      el('header', {}, el('span', { class: `dot ${dotFor[c.state]}` }), el('span', { text: c.label }), el('span', { class: `badge badge--${c.state === 'unknown' ? 'neutral' : c.state === 'bad' ? 'bad' : c.state === 'warn' ? 'warn' : 'ok'}`, text: wordFor[c.state], attrs: { style: 'margin-left:auto' } })),
      el('div', { class: 'kv', text: c.detail }))));
  } catch (err) {
    clear(banner).append(el('div', { class: 'callout callout--bad', attrs: { role: 'alert' } }, icon('alert'), el('div', {}, el('div', { text: `Backend unreachable or returned an error: ${describeError(err)}` }),
      el('div', { class: 'small', text: 'Check that the backend is running and, if the frontend is served separately, the API base URL in Settings.' }))));
    clear(grid);
  }
  stamp.textContent = lastOk ? `Last successful check: ${formatTime(lastOk)}` : 'No successful check yet';
  if (auto.checked) timer = setTimeout(() => { if (!document.hidden) load(); else timer = setTimeout(load, TIMING.healthPollMs); }, TIMING.healthPollMs);
}
refresh.addEventListener('click', load);
auto.addEventListener('change', () => { clearTimeout(timer); if (auto.checked) load(); });
globalThis.addEventListener?.('pagehide', () => clearTimeout(timer));
load();
