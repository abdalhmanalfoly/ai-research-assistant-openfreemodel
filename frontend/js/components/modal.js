import { el } from '../utils/dom.js';
import { icon } from './icons.js';

let seq = 0;
/** Accessible modal built on the native <dialog> (focus trap, Esc to close, backdrop click to close). */
export function openModal({ title, body, footer = [], onClose } = {}) {
  const id = `modal-title-${++seq}`;
  const closeBtn = el('button', { class: 'btn btn--ghost btn--icon', type: 'button', attrs: { 'aria-label': 'Close dialog' } }, icon('close'));
  const dialog = el('dialog', { class: 'modal', attrs: { 'aria-labelledby': id } },
    el('div', { class: 'modal-head' }, el('h2', { text: title, attrs: { id } }), closeBtn),
    el('div', { class: 'modal-body' }, body),
    footer.length ? el('div', { class: 'modal-foot' }, footer) : null);
  const close = () => dialog.open && dialog.close();
  closeBtn.addEventListener('click', close);
  dialog.addEventListener('click', (e) => { if (e.target === dialog) close(); });
  dialog.addEventListener('close', () => { dialog.remove(); onClose?.(); });
  document.body.append(dialog);
  dialog.showModal();
  return { dialog, close };
}

export function confirmDialog({ title, message, confirmText = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    let answer = false;
    const cancel = el('button', { class: 'btn', type: 'button', text: 'Cancel' });
    const ok = el('button', { class: `btn ${danger ? 'btn--danger' : 'btn--primary'}`, type: 'button', text: confirmText });
    const m = openModal({ title, body: el('p', { text: message }), footer: [cancel, ok], onClose: () => resolve(answer) });
    cancel.addEventListener('click', m.close);
    ok.addEventListener('click', () => { answer = true; m.close(); });
  });
}
