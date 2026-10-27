import { mountShell } from '../components/sidebar.js';
import { el, clear, copyText, qs } from '../utils/dom.js';
import { icon } from '../components/icons.js';
import { toast } from '../components/toast.js';
import { confirmDialog } from '../components/modal.js';
import { renderMarkdown } from '../utils/markdown.js';
import { renderCitations, focusCitation } from '../components/citation.js';
import { askQuestion } from '../api/chat.js';
import { listPapers } from '../api/papers.js';
import { describeError, ApiError } from '../api/client.js';
import { validateQuestion } from '../utils/validation.js';
import { describeWarning } from '../utils/format.js';
import { STORAGE_KEYS, LIMITS, getPref, setPref } from '../config.js';

const { actions } = mountShell({ page: 'chat', title: 'Research Chat' });
const root = qs('#page-content');

/* ---- state: kept client-side only (the backend has no conversations/memory) ---- */
const session = globalThis.sessionStorage;
const state = {
  messages: loadHistory(), busy: false, abort: null,
  scope: { paperIds: new Set(), yearMin: null, yearMax: null, topK: null },
  papers: null, papersTotal: 0,
};
const preselected = new URLSearchParams(location.search).get('paper');
if (preselected) state.scope.paperIds.add(preselected);
let seq = state.messages.length;

function loadHistory() {
  try { const v = JSON.parse(getPref(STORAGE_KEYS.chat, '[]', session)); return Array.isArray(v) ? v : []; } catch { return []; }
}
const saveHistory = () => setPref(STORAGE_KEYS.chat, JSON.stringify(state.messages.slice(-LIMITS.chatMessagesMax)), session);

/* ---- layout ---- */
const list = el('div', { class: 'chat-inner', attrs: { role: 'log', 'aria-live': 'polite', 'aria-label': 'Conversation' } });
const scroller = el('div', { class: 'chat-scroll' }, list);
const textarea = el('textarea', { class: 'textarea', attrs: { rows: 1, placeholder: 'Ask about the papers in your library…', 'aria-label': 'Your question', maxlength: LIMITS.questionMax } });
const sendBtn = el('button', { class: 'btn btn--primary btn--icon', type: 'button', attrs: { 'aria-label': 'Send question' } }, icon('send'));
const counter = el('span', { class: 'hint', text: '' });
const scopeBtn = el('button', { class: 'btn btn--sm', type: 'button', attrs: { 'aria-expanded': 'false', 'aria-controls': 'scope-panel' } }, 'Scope');
const scopePanel = el('div', { class: 'scope-panel', hidden: true, attrs: { id: 'scope-panel' } });
root.replaceChildren(scroller, el('div', { class: 'composer-wrap' }, el('div', { class: 'composer-inner' },
  scopePanel, el('div', { class: 'composer' }, textarea, sendBtn),
  el('div', { class: 'row row--between' }, el('div', { class: 'row' }, scopeBtn, counter),
    el('span', { class: 'hint', text: 'Enter to send · Shift+Enter for a new line' })))));
root.classList.add('page--chat');

const newChatBtn = el('button', { class: 'btn btn--sm', type: 'button' }, icon('plus'), 'New chat');
actions.append(newChatBtn);

/* ---- rendering ---- */
function intro() {
  return el('div', { class: 'chat-intro' }, el('h2', { text: 'Ask your research library' }),
    el('p', { class: 'muted', text: 'Answers are generated from passages retrieved from the indexed papers, with citations to the source paper and page.' }),
    el('div', { class: 'callout callout--info', attrs: { style: 'text-align:left' } }, icon('info'),
      el('div', { text: 'Each question is answered on its own: the assistant does not remember earlier messages, and conversations are not saved on the server. This tab keeps the visible history only until you close it.' })));
}

function assistantNode(m) {
  if (m.error) {
    const retry = el('button', { class: 'btn btn--sm', type: 'button', on: { click: () => retryMessage(m.id) } }, icon('refresh'), 'Retry');
    return el('div', { class: 'msg msg--assistant' }, el('div', { class: 'callout callout--bad', attrs: { role: 'alert' } }, icon('alert'),
      el('div', {}, el('div', { text: m.error }), el('div', { class: 'row', attrs: { style: 'margin-top:.5rem' } }, retry))));
  }
  const r = m.result;
  const expanded = getPref(STORAGE_KEYS.citationsExpanded, '0') === '1';
  const answer = el('div', { class: 'answer' }, renderMarkdown(r.answer, { citationIds: r.citations.map((c) => c.id), onCite: (id) => focusCitation(m.id, id) }));
  const copy = el('button', { class: 'btn btn--ghost btn--sm', type: 'button', on: { click: async () => toast((await copyText(r.answer)) ? 'Answer copied' : 'Could not copy', { type: 'info' }) } }, icon('copy'), 'Copy answer');
  const notes = [];
  if (r.insufficientEvidence) notes.push(el('div', { class: 'callout callout--warn' }, icon('alert'), el('div', { text: 'Insufficient evidence: the backend did not find enough relevant passages in the indexed papers. The text above is not a research-backed answer.' })));
  else if (!r.citations.length) notes.push(el('div', { class: 'callout callout--info' }, icon('info'), el('div', { text: 'No citations were returned for this answer. Treat it as unverified.' })));
  for (const w of r.warnings) notes.push(el('div', { class: 'callout callout--warn' }, icon('alert'), el('div', { text: describeWarning(w) })));
  const meta = [r.model ? `Model: ${r.model}` : null, `${r.retrievedChunks} evidence passage${r.retrievedChunks === 1 ? '' : 's'} used`].filter(Boolean).join(' · ');
  return el('div', { class: 'msg msg--assistant' }, answer, notes, renderCitations(r.citations, { expanded, msgId: m.id }),
    el('div', { class: 'msg-meta' }, el('span', { text: meta }), el('span', { class: 'msg-actions' }, copy)));
}

function render() {
  clear(list);
  if (!state.messages.length) list.append(intro());
  for (const m of state.messages) {
    list.append(m.role === 'user' ? el('div', { class: 'msg msg--user' }, el('div', { class: 'bubble-user', text: m.content })) : assistantNode(m));
  }
  if (state.busy) list.append(el('div', { class: 'msg msg--assistant typing', attrs: { role: 'status' } }, el('span', { class: 'spinner' }), 'Searching the papers and writing an answer… this can take a while on a local model.'));
  sendBtn.replaceChildren(icon(state.busy ? 'stop' : 'send'));
  sendBtn.setAttribute('aria-label', state.busy ? 'Stop waiting' : 'Send question');
  scroller.scrollTop = scroller.scrollHeight;
}

/* ---- sending ---- */
async function ask(question, { addUser = true } = {}) {
  if (state.busy) return;
  if (addUser) state.messages.push({ id: `m${++seq}`, role: 'user', content: question });
  state.busy = true; state.abort = new AbortController(); render();
  const id = `m${++seq}`;
  try {
    const result = await askQuestion({ question, paperIds: [...state.scope.paperIds], topK: state.scope.topK ?? undefined, yearMin: state.scope.yearMin ?? undefined, yearMax: state.scope.yearMax ?? undefined }, { signal: state.abort.signal });
    state.messages.push({ id, role: 'assistant', result });
  } catch (err) {
    if (err instanceof ApiError && err.kind === 'aborted') {
      toast('Stopped waiting. The server may still finish generating in the background.', { type: 'warn' });
      if (addUser) state.messages.pop(); // nothing was answered; drop the unanswered question
    } else state.messages.push({ id, role: 'assistant', error: describeError(err), question });
  } finally { state.busy = false; state.abort = null; saveHistory(); render(); }
}

function retryMessage(id) {
  const i = state.messages.findIndex((m) => m.id === id);
  const q = state.messages[i]?.question;
  if (!q) return;
  state.messages.splice(i, 1);
  ask(q, { addUser: false }); // chat is read-only on the server, so retrying is safe
}

function submit() {
  if (state.busy) { state.abort?.abort(); return; }
  const v = validateQuestion(textarea.value, LIMITS.questionMax);
  if (!v.ok) { toast(v.error, { type: 'warn' }); return; }
  textarea.value = ''; autosize(); ask(v.value);
}

function autosize() {
  textarea.style.height = 'auto'; textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
  counter.textContent = textarea.value.length > LIMITS.questionMax * 0.8 ? `${textarea.value.length}/${LIMITS.questionMax}` : '';
}
textarea.addEventListener('input', autosize);
textarea.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); } });
sendBtn.addEventListener('click', submit);
newChatBtn.addEventListener('click', async () => {
  if (state.messages.length && !(await confirmDialog({ title: 'Start a new chat?', message: 'The visible history in this tab will be cleared. Nothing is stored on the server.', confirmText: 'Clear and start new', danger: true }))) return;
  state.abort?.abort(); state.messages = []; saveHistory(); render(); textarea.focus();
});

/* ---- scope panel (paper / year / top-k filters sent to the API) ---- */
function activeFilters() { const s = state.scope; return s.paperIds.size + (s.yearMin ? 1 : 0) + (s.yearMax ? 1 : 0) + (s.topK ? 1 : 0); }
function paintScopeBtn() { const n = activeFilters(); scopeBtn.replaceChildren(icon('search'), n ? `Scope (${n})` : 'Scope'); }

async function buildScope() {
  const num = (v) => (v === '' ? null : Number.parseInt(v, 10));
  const topK = el('select', { class: 'select', attrs: { id: 'topk' } }, el('option', { value: '', text: 'Server default' }), [3, 5, 8, 10, 15, 20].map((n) => el('option', { value: String(n), text: String(n), selected: state.scope.topK === n })));
  const yMin = el('input', { class: 'input', type: 'number', value: state.scope.yearMin ?? '', attrs: { id: 'ymin', min: 1000, max: 3000, placeholder: 'e.g. 2018' } });
  const yMax = el('input', { class: 'input', type: 'number', value: state.scope.yearMax ?? '', attrs: { id: 'ymax', min: 1000, max: 3000, placeholder: 'e.g. 2024' } });
  topK.addEventListener('change', () => { state.scope.topK = num(topK.value); paintScopeBtn(); });
  yMin.addEventListener('change', () => { state.scope.yearMin = num(yMin.value); paintScopeBtn(); });
  yMax.addEventListener('change', () => { state.scope.yearMax = num(yMax.value); paintScopeBtn(); });
  const picks = el('div', { class: 'paper-picks', attrs: { role: 'group', 'aria-label': 'Limit to papers' } }, el('span', { class: 'hint', text: 'Loading papers…' }));
  scopePanel.replaceChildren(
    el('div', { class: 'field' }, el('label', { text: 'Evidence passages (top-k)', attrs: { for: 'topk' } }), topK),
    el('div', { class: 'field' }, el('label', { text: 'Published from', attrs: { for: 'ymin' } }), yMin),
    el('div', { class: 'field' }, el('label', { text: 'Published until', attrs: { for: 'ymax' } }), yMax), picks);
  try {
    if (!state.papers) { const r = await listPapers({ limit: 100, status: 'indexed' }); state.papers = r.items; state.papersTotal = r.total; }
    clear(picks);
    if (!state.papers.length) picks.append(el('span', { class: 'hint', text: 'No indexed papers yet. Upload papers first.' }));
    for (const p of state.papers) {
      const cb = el('input', { type: 'checkbox', checked: state.scope.paperIds.has(p.id) });
      cb.addEventListener('change', () => { cb.checked ? state.scope.paperIds.add(p.id) : state.scope.paperIds.delete(p.id); paintScopeBtn(); });
      picks.append(el('label', {}, cb, el('span', { text: p.title })));
    }
    if (state.papersTotal > state.papers.length) picks.append(el('span', { class: 'hint', text: `Showing the first ${state.papers.length} of ${state.papersTotal} indexed papers.` }));
    else if (state.papers.length) picks.append(el('span', { class: 'hint', text: 'Leave all unchecked to search every indexed paper.' }));
  } catch (err) { clear(picks); picks.append(el('span', { class: 'field-error', text: `Could not load papers: ${describeError(err)}` })); }
}
scopeBtn.addEventListener('click', () => {
  const open = scopePanel.hidden; scopePanel.hidden = !open; scopeBtn.setAttribute('aria-expanded', String(open));
  if (open) buildScope();
});

paintScopeBtn(); render(); autosize();
