import { STORAGE_KEYS, getPref, setPref } from '../config.js';

export const THEMES = ['system', 'light', 'dark'];
export const resolveTheme = (pref, prefersDark) => (pref === 'dark' || (pref !== 'light' && prefersDark) ? 'dark' : 'light');
export const getThemePref = (storage) => { const v = getPref(STORAGE_KEYS.theme, 'system', storage); return THEMES.includes(v) ? v : 'system'; };
const systemDark = () => !!globalThis.matchMedia?.('(prefers-color-scheme: dark)').matches;

export function applyTheme(pref = getThemePref()) {
  const theme = resolveTheme(pref, systemDark());
  document.documentElement.setAttribute('data-theme', theme);
  return theme;
}
export function setThemePref(pref, storage) {
  const p = THEMES.includes(pref) ? pref : 'system';
  setPref(STORAGE_KEYS.theme, p, storage);
  return applyTheme(p);
}
/** Header button: flips between light and dark explicitly. */
export function toggleTheme(storage) {
  const current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  return setThemePref(current === 'dark' ? 'light' : 'dark', storage);
}
