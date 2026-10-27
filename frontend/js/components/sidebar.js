import { el, qs } from '../utils/dom.js';
import { icon } from './icons.js';
import { applyTheme, toggleTheme } from './theme.js';
import { initToastHost } from './toast-host.js';
import { getHealth, describeHealth } from '../api/health.js';
import { STORAGE_KEYS, TIMING, getPref, setPref } from '../config.js';

const NAV = [
  ['chat', 'Research Chat', 'chat', 'chat.html'], ['papers', 'Research Library', 'library', 'papers.html'],
  ['upload', 'Upload Papers', 'upload', 'upload.html'], ['jobs', 'Processing Jobs', 'jobs', 'jobs.html'],
  ['status', 'System Status', 'status', 'status.html'], ['settings', 'Settings', 'settings', 'settings.html'],
];

/** Builds sidebar + header around the page's own <main id="page-content">. Returns {main, actions}. */
export function mountShell({ page, title }) {
  applyTheme();
  initToastHost();
  document.title = `${title} · AI Research Assistant`;
  const shell = qs('#app-shell');
  const main = qs('#page-content');
  const collapsed = getPref(STORAGE_KEYS.sidebar, '0') === '1';
  shell.classList.toggle('is-collapsed', collapsed);

  const nav = el('ul', { class: 'nav' }, NAV.map(([key, label, ic, href]) => el('li', {},
    el('a', { href, attrs: { 'aria-current': key === page ? 'page' : null, title: label } }, icon(ic), el('span', { class: 'nav-label', text: label })))));
  const collapseBtn = el('button', { class: 'btn btn--ghost collapse-toggle', type: 'button', attrs: { 'aria-pressed': collapsed, 'aria-label': 'Collapse sidebar' } },
    icon('collapse'), el('span', { class: 'collapse-label', text: 'Collapse' }));
  const sidebar = el('aside', { class: 'sidebar', attrs: { id: 'sidebar', 'aria-label': 'Primary' } },
    el('a', { class: 'brand', href: 'chat.html', attrs: { 'aria-label': 'AI Research Assistant home' } },
      el('span', { class: 'brand-mark' }, icon('library')), el('span', { class: 'brand-name', text: 'Research Assistant' })),
    el('nav', { attrs: { 'aria-label': 'Main' } }, nav), el('div', { class: 'sidebar-footer' }, collapseBtn));
  const overlay = el('div', { class: 'overlay', hidden: true });

  const menuBtn = el('button', { class: 'btn btn--ghost btn--icon menu-btn', type: 'button', attrs: { 'aria-label': 'Open navigation', 'aria-controls': 'sidebar', 'aria-expanded': 'false' } }, icon('menu'));
  const pill = el('a', { class: 'badge health-pill', href: 'status.html', text: 'Checking…', attrs: { title: 'Backend status' } });
  const themeBtn = el('button', { class: 'btn btn--ghost btn--icon', type: 'button', attrs: { 'aria-label': 'Toggle light/dark theme' } });
  const actions = el('div', { class: 'header-actions' });
  const header = el('header', { class: 'header' }, menuBtn, el('h1', { text: title }), actions, pill, themeBtn);
  const paintTheme = () => themeBtn.replaceChildren(icon(document.documentElement.getAttribute('data-theme') === 'dark' ? 'sun' : 'moon'));
  paintTheme();
  themeBtn.addEventListener('click', () => { toggleTheme(); paintTheme(); });

  shell.replaceChildren(sidebar, overlay, el('div', { class: 'main-col' }, header, main));

  const setDrawer = (open) => {
    const was = sidebar.classList.contains('is-open');
    sidebar.classList.toggle('is-open', open); overlay.hidden = !open;
    menuBtn.setAttribute('aria-expanded', String(open));
    if (open) qs('a', sidebar)?.focus(); else if (was) menuBtn.focus();
  };
  menuBtn.addEventListener('click', () => setDrawer(!sidebar.classList.contains('is-open')));
  overlay.addEventListener('click', () => setDrawer(false));
  sidebar.addEventListener('click', (e) => { if (e.target.closest('a')) setDrawer(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && sidebar.classList.contains('is-open')) setDrawer(false); });
  globalThis.matchMedia?.('(min-width: 801px)').addEventListener?.('change', (e) => { if (e.matches) setDrawer(false); });
  collapseBtn.addEventListener('click', () => {
    const c = !shell.classList.contains('is-collapsed');
    shell.classList.toggle('is-collapsed', c); collapseBtn.setAttribute('aria-pressed', String(c)); setPref(STORAGE_KEYS.sidebar, c ? '1' : '0');
  });

  startHealthPill(pill);
  return { main, actions };
}

function startHealthPill(pill) {
  const paint = (cls, label, title) => { pill.className = `badge health-pill ${cls}`; pill.textContent = label; pill.title = title; };
  let timer;
  async function check() {
    if (document.hidden) return;
    try {
      const d = describeHealth(await getHealth({ timeoutMs: 8000 }));
      d.overall === 'ready' ? paint('badge--ok', 'Ready', 'All reported checks pass') : paint('badge--warn', 'Degraded', 'API reachable but some checks are not OK. Click for details.');
    } catch { paint('badge--bad', 'Offline', 'Backend not reachable'); }
  }
  check();
  timer = setInterval(check, TIMING.healthPollMs);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
  globalThis.addEventListener?.('pagehide', () => clearInterval(timer));
}
