import { el } from '../utils/dom.js';
/* Creates the (empty) live region early so screen readers register it before the first toast. */
export function initToastHost() {
  if (!document.querySelector('.toasts')) document.body.append(el('div', { class: 'toasts', attrs: { role: 'region', 'aria-label': 'Notifications', 'aria-live': 'polite' } }));
}
