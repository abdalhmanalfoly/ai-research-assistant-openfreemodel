import { renderMarkdown } from '../../js/utils/markdown.js';
import { renderCitationCard } from '../../js/components/citation.js';
import { el } from '../../js/utils/dom.js';
import { toggleTheme, setThemePref } from '../../js/components/theme.js';
import { mountShell } from '../../js/components/sidebar.js';

const results = document.getElementById('results');
let pass = 0, total = 0;
async function test(name, fn) {
  total++;
  try { await fn(); pass++; results.append(el('li', { class: 'ok', text: `PASS  ${name}` })); }
  catch (e) { results.append(el('li', { class: 'fail', text: `FAIL  ${name}: ${e.message}` })); }
}
const assert = (c, m = 'assertion failed') => { if (!c) throw new Error(m); };
const mem = () => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }; };
const wrap = (frag) => { const d = document.createElement('div'); d.append(frag); return d; };

await test('el(): HTML in text stays text', () => {
  const n = el('div', { text: '<img src=x onerror=alert(1)>' });
  assert(n.children.length === 0 && n.textContent.includes('<img'));
});
await test('markdown: hostile input produces no script/img/iframe/anchor elements', () => {
  const d = wrap(renderMarkdown('<script>alert(1)</script> <img src=x onerror=alert(1)> [x](javascript:alert(1)) <iframe src=//evil></iframe>'));
  assert(!d.querySelector('script, img, iframe, a'), 'dangerous element created');
  assert(d.textContent.includes('<script>'), 'raw text should remain visible');
});
await test('markdown: only https links become anchors, with rel=noopener', () => {
  const a = wrap(renderMarkdown('[ok](https://example.org/p)')).querySelector('a');
  assert(a && a.rel.includes('noopener') && a.target === '_blank');
});
await test('markdown: known citation ids become buttons; unknown stay text; click fires', () => {
  let clicked = null;
  const d = wrap(renderMarkdown('Claim [E1]. Fake [E9].', { citationIds: ['E1'], onCite: (id) => { clicked = id; } }));
  const btns = d.querySelectorAll('button.cite-ref');
  assert(btns.length === 1 && btns[0].textContent === 'E1' && d.textContent.includes('[E9]'));
  btns[0].click(); assert(clicked === 'E1');
});
await test('citation card: unknown page is stated, excerpt rendered as text only', () => {
  const card = renderCitationCard({ id: 'E1', paperId: 'p', title: 'T', authors: [], pageNumber: null, section: null, doi: 'javascript:x', sourceUrl: 'javascript:x', relevanceScore: 0.5, excerpt: '<b>bold</b> text' }, { expanded: true });
  assert(card.textContent.includes('page not available'));
  assert(!card.querySelector('b'), 'excerpt HTML must not be parsed');
  assert(![...card.querySelectorAll('a')].some((a) => a.href.startsWith('javascript:')), 'unsafe link rendered');
});
await test('theme: toggle flips data-theme and persists preference', () => {
  const s = mem();
  setThemePref('light', s);
  assert(document.documentElement.getAttribute('data-theme') === 'light');
  toggleTheme(s);
  assert(document.documentElement.getAttribute('data-theme') === 'dark' && s.getItem('ara.theme') === 'dark');
});
await test('shell: mobile drawer opens/closes with button and Escape; nav marks current page', () => {
  const shell = el('div', { attrs: { id: 'app-shell' } }, el('main', { attrs: { id: 'page-content' } }));
  document.body.append(shell);
  mountShell({ page: 'papers', title: 'Test' });
  const menu = shell.querySelector('.menu-btn'), sb = shell.querySelector('#sidebar');
  menu.click(); assert(sb.classList.contains('is-open') && menu.getAttribute('aria-expanded') === 'true');
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); assert(!sb.classList.contains('is-open'));
  assert(shell.querySelector('a[aria-current="page"]').getAttribute('href') === 'papers.html');
  shell.remove();
});

document.title = `${pass === total ? 'PASS' : 'FAIL'} ${pass}/${total}`;
results.before(el('p', { class: pass === total ? 'ok' : 'fail', text: `${pass}/${total} passed` }));
