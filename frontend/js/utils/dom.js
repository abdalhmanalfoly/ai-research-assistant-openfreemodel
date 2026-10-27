/* Tiny DOM helpers. Text is ALWAYS set via textContent / text nodes, never innerHTML. */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  const { class: cls, text, attrs, dataset, on, ...rest } = props || {};
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (attrs) for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? '' : String(v));
  if (dataset) for (const [k, v] of Object.entries(dataset)) node.dataset[k] = String(v);
  if (on) for (const [evt, fn] of Object.entries(on)) node.addEventListener(evt, fn);
  for (const [k, v] of Object.entries(rest)) node[k] = v; // e.g. disabled, hidden, value, type
  append(node, children);
  return node;
}
export function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
export function clear(node) { node.replaceChildren(); return node; }
export const qs = (sel, root = document) => root.querySelector(sel);
export const qsa = (sel, root = document) => [...root.querySelectorAll(sel)];
export function debounce(fn, ms = 300) {
  let t; const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; d.cancel = () => clearTimeout(t); return d;
}
export function skeletonRows(n = 4) {
  return el('div', { class: 'stack', attrs: { 'aria-hidden': 'true' } },
    ...Array.from({ length: n }, () => el('div', { class: 'skeleton', attrs: { style: 'height:2.4rem' } })));
}

/** Copies text; falls back to a hidden textarea where the async Clipboard API is unavailable (e.g. plain http). */
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
  const ta = el('textarea', { value: text, attrs: { readonly: true, style: 'position:fixed;opacity:0' } });
  document.body.append(ta); ta.select();
  let ok = false; try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove(); return ok;
}
