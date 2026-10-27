import { el } from '../utils/dom.js';
import { formatAuthors, formatScore } from '../utils/format.js';
import { doiUrl, safeHttpUrl } from '../utils/validation.js';
import { openModal } from './modal.js';

export const citationCardId = (msgId, citId) => `cite-${msgId}-${citId}`;

function links(c) {
  const out = [];
  const d = doiUrl(c.doi), u = safeHttpUrl(c.sourceUrl);
  if (d) out.push(el('a', { text: 'DOI', attrs: { href: d, target: '_blank', rel: 'noopener noreferrer' } }));
  if (u) out.push(el('a', { text: 'Source', attrs: { href: u, target: '_blank', rel: 'noopener noreferrer' } }));
  if (c.paperId) out.push(el('a', { text: 'Open in library', attrs: { href: `papers.html?paper=${encodeURIComponent(c.paperId)}` } }));
  return out;
}

/** One evidence/citation card. Only fields the backend returned are shown; unknown page is stated, never guessed. */
export function renderCitationCard(c, { expanded = false, msgId = 'm' } = {}) {
  const score = formatScore(c.relevanceScore);
  const sub = [formatAuthors(c.authors), c.pageNumber !== null ? `p. ${c.pageNumber}` : 'page not available', c.section, score ? `relevance ${score}` : null].filter(Boolean).join(' · ');
  const title = el('button', { class: 'link-btn cite-title', type: 'button', text: c.title, attrs: { 'aria-haspopup': 'dialog' }, on: { click: () => openCitationDialog(c) } });
  const excerpt = c.excerpt
    ? el('details', { open: expanded }, el('summary', { class: 'small muted', text: 'Retrieved passage' }), el('p', { class: 'cite-excerpt', text: c.excerpt }))
    : null;
  const l = links(c);
  return el('article', { class: 'cite-card', attrs: { id: citationCardId(msgId, c.id) } },
    el('div', { class: 'cite-top' }, el('span', { class: 'cite-id', text: c.id }), el('div', {}, title, el('div', { class: 'cite-sub', text: sub }))),
    excerpt,
    l.length ? el('div', { class: 'row small' }, l) : null);
}

export function renderCitations(citations, opts) {
  if (!citations.length) return null;
  return el('section', { class: 'citations', attrs: { 'aria-label': 'Citations' } },
    el('div', { class: 'citations-head', text: `Sources (${citations.length})` }),
    citations.map((c) => renderCitationCard(c, opts)));
}

export function focusCitation(msgId, citId) {
  const card = document.getElementById(citationCardId(msgId, citId));
  if (!card) return;
  card.querySelector('details')?.setAttribute('open', '');
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.classList.remove('is-flash'); void card.offsetWidth; card.classList.add('is-flash');
}

export function openCitationDialog(c) {
  const rows = [['Citation', c.id], ['Paper', c.title], ['Authors', c.authors.join(', ') || '—'],
    ['Page', c.pageNumber !== null ? String(c.pageNumber) : 'Not available'], ['Section', c.section ?? '—'],
    ['Relevance', formatScore(c.relevanceScore) ?? '—'], ['DOI', c.doi ?? '—']];
  const dl = el('dl', { class: 'dl' }, rows.map(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
  const l = links(c);
  return openModal({ title: `Citation ${c.id}`, body: el('div', { class: 'stack' }, dl,
    c.excerpt ? el('div', {}, el('div', { class: 'label', text: 'Retrieved passage' }), el('p', { class: 'cite-excerpt', text: c.excerpt })) : null,
    el('p', { class: 'hint', text: 'The passage is the text the model was shown. A valid citation shows which passage was supplied, not that it supports every statement.' }),
    l.length ? el('div', { class: 'row' }, l) : null) });
}
