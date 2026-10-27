import { el } from '../utils/dom.js';
import { icon } from './icons.js';

let host;
function getHost() {
  if (!host || !host.isConnected) host = document.querySelector('.toasts');
  if (!host) { host = el('div', { class: 'toasts', attrs: { role: 'region', 'aria-label': 'Notifications', 'aria-live': 'polite' } }); document.body.append(host); }
  return host;
}
/** toast('Saved', {type: 'ok'|'error'|'warn'|'info', timeout: ms (0 = sticky)}) */
export function toast(message, { type = 'info', timeout = type === 'error' ? 8000 : 4000 } = {}) {
  const close = el('button', { class: 'btn btn--ghost btn--icon btn--sm', type: 'button', attrs: { 'aria-label': 'Dismiss' } }, icon('close'));
  const node = el('div', { class: `toast toast--${type}`, attrs: { role: type === 'error' ? 'alert' : 'status' } }, el('div', { text: message }), close);
  const remove = () => node.remove();
  close.addEventListener('click', remove);
  getHost().append(node);
  if (timeout) setTimeout(remove, timeout);
  return remove;
}
