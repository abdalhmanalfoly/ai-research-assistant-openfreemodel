/* Safe, limited Markdown renderer for model output.
 * Stage 1 (pure, unit-tested): text -> block/inline tokens.
 * Stage 2: tokens -> DOM nodes built with createElement/textContent. innerHTML is never used, so raw HTML in model
 * output is displayed as text and cannot execute. Only http(s) links are produced.
 * Supported: paragraphs, #..#### headings, -,*,1. lists, fenced code, **bold**, *italic*, `code`, [text](https://...),
 * and citation markers like [E1] / [E1, E2] (only for ids present in `citationIds`).
 */
import { el } from './dom.js';

const CITE_GROUP = /^\[\s*(E\d+(?:\s*[,;]\s*E\d+)*)\s*\]$/i;
const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\s][^*\n]*\*)|(\[\s*E\d+(?:\s*[,;]\s*E\d+)*\s*\])|(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/gi;

export function parseInline(text, citationIds = new Set()) {
  const out = [];
  let last = 0;
  const push = (v) => { if (v) out.push({ t: 'text', v }); };
  for (const m of String(text).matchAll(INLINE)) {
    push(text.slice(last, m.index));
    const [raw] = m;
    if (m[1]) out.push({ t: 'code', v: raw.slice(1, -1) });
    else if (m[2]) out.push({ t: 'strong', children: parseInline(raw.slice(2, -2), citationIds) });
    else if (m[3]) out.push({ t: 'em', children: parseInline(raw.slice(1, -1), citationIds) });
    else if (m[4]) {
      const ids = (CITE_GROUP.exec(raw)?.[1].match(/E\d+/gi) ?? []).map((s) => s.toUpperCase());
      const known = ids.filter((id) => citationIds.has(id));
      if (known.length) out.push({ t: 'cite', ids: known });
      else push(raw); // unknown ids stay visible as plain text; never turned into fake citations
    } else if (m[5]) {
      const lm = /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/i.exec(raw);
      out.push({ t: 'link', href: lm[2], children: parseInline(lm[1], citationIds) });
    }
    last = m.index + raw.length;
  }
  push(text.slice(last));
  return out;
}

export function parseBlocks(text) {
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
  const blocks = [];
  let para = [];
  let i = 0;
  const flush = () => { if (para.length) { blocks.push({ type: 'p', text: para.join(' ') }); para = []; } };
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      const lang = line.replace(/^\s*```/, '').trim();
      const code = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i++;
      blocks.push({ type: 'code', lang, text: code.join('\n') });
      continue;
    }
    const h = /^\s{0,3}(#{1,4})\s+(.*)$/.exec(line);
    if (h) { flush(); blocks.push({ type: 'h', level: h[1].length, text: h[2].trim() }); i++; continue; }
    const ul = /^\s*[-*•]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ul || ol) {
      flush();
      const type = ul ? 'ul' : 'ol';
      const re = ul ? /^\s*[-*•]\s+(.*)$/ : /^\s*\d+[.)]\s+(.*)$/;
      const items = [];
      while (i < lines.length && re.test(lines[i])) items.push(re.exec(lines[i++])[1]);
      blocks.push({ type, items });
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    para.push(line.trim());
    i++;
  }
  flush();
  return blocks;
}

function inlineNodes(tokens, onCite) {
  return tokens.map((tk) => {
    switch (tk.t) {
      case 'text': return document.createTextNode(tk.v);
      case 'code': return el('code', { text: tk.v });
      case 'strong': return el('strong', {}, inlineNodes(tk.children, onCite));
      case 'em': return el('em', {}, inlineNodes(tk.children, onCite));
      case 'link': return el('a', { attrs: { href: tk.href, target: '_blank', rel: 'noopener noreferrer nofollow' } }, inlineNodes(tk.children, onCite));
      case 'cite': return el('span', {}, tk.ids.map((id) => el('button', {
        class: 'cite-ref', type: 'button', text: id, attrs: { 'aria-label': `Show citation ${id}` },
        on: { click: () => onCite?.(id) },
      })));
      default: return null;
    }
  });
}

export function renderMarkdown(text, { citationIds = [], onCite } = {}) {
  const ids = new Set([...citationIds].map((s) => String(s).toUpperCase()));
  const frag = document.createDocumentFragment();
  const inl = (s) => inlineNodes(parseInline(s, ids), onCite);
  for (const b of parseBlocks(text)) {
    if (b.type === 'p') frag.append(el('p', {}, inl(b.text)));
    else if (b.type === 'h') frag.append(el(b.level <= 2 ? 'h3' : 'h4', {}, inl(b.text)));
    else if (b.type === 'code') frag.append(el('pre', {}, el('code', { text: b.text })));
    else frag.append(el(b.type, {}, b.items.map((it) => el('li', {}, inl(it)))));
  }
  return frag;
}
