import { mountShell } from '../components/sidebar.js';
import { el, qs } from '../utils/dom.js';
import { icon } from '../components/icons.js';
import { toast } from '../components/toast.js';
import { confirmDialog } from '../components/modal.js';
import { THEMES, getThemePref, setThemePref } from '../components/theme.js';
import { getHealth } from '../api/health.js';
import { describeError } from '../api/client.js';
import { DEFAULT_API_BASE, STORAGE_KEYS, getApiBase, getPref, normalizeApiBase, setPref } from '../config.js';

mountShell({ page: 'settings', title: 'Settings' });
const root = qs('#page-content');

const themeBtns = THEMES.map((t) => el('button', { type: 'button', text: t[0].toUpperCase() + t.slice(1), attrs: { 'aria-pressed': String(getThemePref() === t) }, dataset: { theme: t } }));
themeBtns.forEach((b) => b.addEventListener('click', () => { setThemePref(b.dataset.theme); themeBtns.forEach((x) => x.setAttribute('aria-pressed', String(x === b))); }));

const sw = (id, key, label, on) => { const i = el('input', { type: 'checkbox', checked: getPref(key, '0') === '1', attrs: { id } }); i.addEventListener('change', () => { setPref(key, i.checked ? '1' : '0'); on?.(i.checked); }); return el('label', { class: 'switch', attrs: { for: id } }, i, label); };

const base = el('input', { class: 'input', attrs: { id: 'apibase', placeholder: 'Same origin (leave empty)', spellcheck: 'false' }, value: getPref(STORAGE_KEYS.apiBase, '') });
const baseErr = el('div', { class: 'field-error' });
const current = el('div', { class: 'hint' });
const paintCurrent = () => { current.textContent = `Effective API base: ${getApiBase() || 'same origin (relative /api/… requests)'}`; };
paintCurrent();
const save = el('button', { class: 'btn btn--primary', type: 'button', text: 'Save', on: { click: () => {
  const n = normalizeApiBase(base.value);
  if (n === null) { baseErr.textContent = 'Use an empty value or a full URL such as http://localhost:8000'; return; }
  baseErr.textContent = ''; setPref(STORAGE_KEYS.apiBase, n || null); paintCurrent(); toast('API base URL saved', { type: 'ok' });
} } });
const test = el('button', { class: 'btn', type: 'button', text: 'Test connection', on: { click: async () => {
  const n = normalizeApiBase(base.value);
  if (n === null) { baseErr.textContent = 'Invalid URL'; return; }
  try { await getHealth({ baseUrl: n || DEFAULT_API_BASE, timeoutMs: 8000 }); toast('Backend reachable', { type: 'ok' }); } catch (e) { toast(describeError(e), { type: 'error' }); }
} } });
const reset = el('button', { class: 'btn btn--ghost', type: 'button', text: 'Reset', on: { click: () => { base.value = ''; setPref(STORAGE_KEYS.apiBase, null); baseErr.textContent = ''; paintCurrent(); } } });

const clearBtn = el('button', { class: 'btn btn--danger', type: 'button' }, icon('trash'), 'Clear chat history in this tab');
clearBtn.addEventListener('click', async () => { if (await confirmDialog({ title: 'Clear chat history?', message: 'This removes the visible conversation stored in this browser tab.', confirmText: 'Clear', danger: true })) { try { globalThis.sessionStorage.removeItem(STORAGE_KEYS.chat); } catch { /* ignore */ } toast('Chat history cleared'); } });

root.replaceChildren(el('div', { class: 'stack page-narrow' },
  el('div', { class: 'callout callout--info' }, icon('info'), el('div', { text: 'These are preferences for this browser only. They do not change the backend’s configuration (model, database, thresholds); that is done with server environment variables.' })),
  el('section', { class: 'card stack' }, el('h3', { text: 'Appearance' }), el('div', { class: 'segmented', attrs: { role: 'group', 'aria-label': 'Theme' } }, themeBtns),
    sw('sb', STORAGE_KEYS.sidebar, 'Start with the sidebar collapsed (desktop)')),
  el('section', { class: 'card stack' }, el('h3', { text: 'Chat' }), sw('ce', STORAGE_KEYS.citationsExpanded, 'Show retrieved passages expanded under each citation'), clearBtn),
  el('section', { class: 'card stack' }, el('h3', { text: 'Backend connection' }),
    el('p', { class: 'muted small', text: 'Leave empty when this app is served by the backend itself. Set a URL (for example http://localhost:8000) only if the frontend is served from another origin; the backend must then list this origin in CORS_ORIGINS. Do not enter secrets: this value is stored in plain text in the browser.' }),
    el('div', { class: 'field' }, el('label', { text: 'API base URL', attrs: { for: 'apibase' } }), base, baseErr, current), el('div', { class: 'row' }, save, test, reset)),
  el('section', { class: 'card stack' }, el('h3', { text: 'Security note' }),
    el('p', { class: 'muted small', text: 'The backend currently has no authentication: anyone who can reach it can ask questions and upload papers. Keep it on a trusted network until login is added.' }))));
